"""
A tour of what dlboard draws, through a Lightning run that uses nothing but stock APIs.

A small UNet learns to segment synthetic shapes (circles, squares, triangles on a noisy background).
Nothing is downloaded, and it trains in a few minutes on a GPU (and a bit longer on a CPU). What it
exercises, all through ordinary Lightning/dlboard calls:

- nested hyperparameters: pydantic models, a pydantic dataclass, a stdlib dataclass and plain args,
  flattened by the logger to `model/base_channels`, `optim/schedule/kind`, ...
- 25 epochs with step- and epoch-level metrics, and per-class IoU
- a histogram of every weight tensor's gradient, so how they spread over training shows without averaging
- Lightning's `LearningRateMonitor` and `ThroughputMonitor`
- `ModelCheckpoint` with `log_model="all"`, so every checkpoint is uploaded as it is saved
- prediction previews every few epochs: segmentation masks and bounding boxes (an image and the `Overlay`s,
  run-length encoded masks included, a chart draws over it)
- Plotly figures, drawn interactive by the `plotly` chart (needs `plotly` installed), in each of its three ways: the
  training set's class balance, logged once; per-class IoU, a figure each epoch the chart scrubs through; and a
  template each epoch's per-sample IoU against foreground area streams into, so the points pile up as training goes

Run it a few times: each run samples fresh hyperparameters, so the run-comparison views have something
to compare. `lightning_stress_test.py` runs the same model logging ten times as long and as often as it can, to
load the server rather than to be looked at. What follows is what dlboard can't do yet, found by running this -- the
backlog this example exists to produce. Where the script itself works around one, a `# DLBOARD GAP:` comment says so:

1. One artifact per (run, key, step), so N previews need N keys (`sample_0`, `sample_1`, ...) rather than
   one batch rendered as a gallery.
"""

import math
import os
import random
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import cast, override

import numpy as np
import plotly.graph_objects as go
import pydantic
import pydantic.dataclasses
import pytorch_lightning as pl
import torch
from pydantic import BaseModel, Field
from pytorch_lightning.callbacks import (
    DeviceStatsMonitor,
    LearningRateMonitor,
    ModelCheckpoint,
    ThroughputMonitor,
)
from pytorch_lightning.utilities.types import OptimizerLRScheduler
from torch import nn
from torch.optim import AdamW, Optimizer
from torch.optim.lr_scheduler import LambdaLR
from torch.utils.data import DataLoader, Dataset
from torchvision.ops import masks_to_boxes

from dlboard.client.artifacts.image import Image
from dlboard.client.artifacts.overlay import Box, ImageMask, Overlay, Shape
from dlboard.client.artifacts.plotly import PlotlyData, PlotlyFigure
from dlboard.client.artifacts.rle import run_length_encode
from dlboard.client.dlboard_logger import DLBoardLogger

LOGDIR = Path("./lightning-logs")
PREVIEW_SAMPLES = 4
"""How many fixed validation images get a segmentation and a detection preview each time."""
CLASS_BALANCE_SAMPLES = 256
"""How many training images the class balance figure counts pixels over."""
SAMPLE_IOU_KEY = "val/sample_iou"
"""The template each validation epoch's per-sample IoU is streamed into."""


class ShapeClass(IntEnum):
    """A pixel's class: the shape drawn on it, or the background."""

    BACKGROUND = 0
    CIRCLE = 1
    SQUARE = 2
    TRIANGLE = 3


FOREGROUND = tuple(c for c in ShapeClass if c is not ShapeClass.BACKGROUND)
NUM_CLASSES = len(ShapeClass)
CLASS_COLORS = {
    int(ShapeClass.CIRCLE): "#e63c3c",
    int(ShapeClass.SQUARE): "#3cc85a",
    int(ShapeClass.TRIANGLE): "#466ef0",
}
"""The color a mask draws each foreground class in, by class id; the background has none, so it stays clear."""
CLASS_NAMES = {int(c): c.name.lower() for c in FOREGROUND}
"""The name of each foreground class, for a mask's legend."""


class Stage(StrEnum):
    """Which loop a step belongs to, and so the prefix its metrics are logged under."""

    TRAIN = "train"
    VAL = "val"
    TEST = "test"


class ScheduleKind(StrEnum):
    """How the learning rate decays after warmup."""

    COSINE = "cosine"
    LINEAR = "linear"
    CONSTANT = "constant"


class NormKind(StrEnum):
    """Which normalization layer the UNet uses."""

    BATCH = "batch"
    GROUP = "group"


@pydantic.dataclasses.dataclass(frozen=True)
class ScheduleConfig:
    """A pydantic dataclass, nested inside `OptimConfig`."""

    kind: ScheduleKind = ScheduleKind.COSINE
    warmup_steps: int = 100


class ModelConfig(BaseModel, frozen=True):
    """UNet shape."""

    base_channels: int = 16
    depth: int = Field(default=3, ge=1, le=4)
    dropout: float = 0.1
    norm: NormKind = NormKind.BATCH


class OptimConfig(BaseModel, frozen=True):
    """Optimizer and schedule."""

    lr: float = 1e-3
    weight_decay: float = 1e-4
    betas: tuple[float, float] = (0.9, 0.999)
    schedule: ScheduleConfig = ScheduleConfig()


@pydantic.dataclasses.dataclass(frozen=True)
class DataConfig:
    """A top-level pydantic dataclass."""

    image_size: int = 96
    num_train: int = 4096
    num_val: int = 512
    batch_size: int = 64


@dataclass(frozen=True)
class AugmentConfig:
    """A plain stdlib dataclass."""

    noise_std: float = 0.05


class Volume(BaseModel, frozen=True):
    """How much a run logs, and how often: not a hyperparameter, so not logged as one."""

    experiment: str
    max_epochs: int
    log_every_n_steps: int
    grad_histogram_every_n_steps: int
    preview_every_n_epochs: int
    device_stats: bool
    """Whether `DeviceStatsMonitor` logs its ~500 series of device and CPU stats."""


TOUR = Volume(
    experiment="unet-advanced",
    max_epochs=25,
    log_every_n_steps=10,
    grad_histogram_every_n_steps=50,
    preview_every_n_epochs=5,
    device_stats=False,
)
"""Enough of each to show it: a slider over five previews, and a chart per gradient that draws at once."""


def render_sample(rng: np.random.Generator, size: int, noise_std: float) -> tuple[np.ndarray, np.ndarray]:
    """One image (3xHxW float in [0, 1]) with 1-3 distinct shapes in random colors, and its HxW class mask."""
    yy, xx = np.mgrid[:size, :size]
    image = np.broadcast_to(rng.uniform(0.1, 0.4, (3, 1, 1)), (3, size, size)).copy()
    mask = np.zeros((size, size), dtype=np.int64)
    for class_id in rng.permutation(np.arange(1, NUM_CLASSES))[: rng.integers(1, NUM_CLASSES)]:
        cx, cy = rng.uniform(0.25 * size, 0.75 * size, 2)
        radius = rng.uniform(0.12 * size, 0.25 * size)
        match ShapeClass(int(class_id)):
            case ShapeClass.CIRCLE:
                region = (xx - cx) ** 2 + (yy - cy) ** 2 <= radius**2
            case ShapeClass.SQUARE:
                region = (abs(xx - cx) <= radius) & (abs(yy - cy) <= radius)
            case ShapeClass.TRIANGLE:
                region = (
                    (yy >= cy - radius) & (yy <= cy + radius) & (abs(xx - cx) <= (yy - (cy - radius)) / 2)
                )
            case ShapeClass.BACKGROUND:
                continue
        image[:, region] = rng.uniform(0.5, 1.0, (3, 1))
        mask[region] = class_id
    image += rng.normal(0, noise_std, image.shape)
    return image.clip(0, 1).astype(np.float32), mask


class ShapesDataset(Dataset[tuple[torch.Tensor, torch.Tensor]]):
    """Shapes drawn on the fly: sample `i` is always the same image, so a split is reproducible."""

    def __init__(self, length: int, size: int, noise_std: float, seed: int) -> None:
        """A split of `length` images, distinguished from the other splits by `seed`."""
        self.length, self.size, self.noise_std, self.seed = length, size, noise_std, seed

    def __len__(self) -> int:
        """Number of images."""
        return self.length

    @override
    def __getitem__(self, index: int) -> tuple[torch.Tensor, torch.Tensor]:
        image, mask = render_sample(np.random.default_rng([self.seed, index]), self.size, self.noise_std)
        return torch.from_numpy(image), torch.from_numpy(mask)


class ShapesDataModule(pl.LightningDataModule):
    """Train/val/test splits of `ShapesDataset`; its init args are logged as hyperparameters too."""

    def __init__(self, data: DataConfig, augment: AugmentConfig, seed: int = 0) -> None:
        """Remember the configs."""
        super().__init__()
        self.save_hyperparameters()
        self.data, self.augment, self.seed = data, augment, seed

    @override
    def setup(self, stage: str | None = None) -> None:
        def split(length: int, offset: int) -> ShapesDataset:
            return ShapesDataset(length, self.data.image_size, self.augment.noise_std, self.seed + offset)

        self.train = split(self.data.num_train, 0)
        self.val = split(self.data.num_val, 1)
        self.test = split(self.data.num_val, 2)

    def _loader(
        self, dataset: ShapesDataset, *, shuffle: bool
    ) -> DataLoader[tuple[torch.Tensor, torch.Tensor]]:
        return DataLoader(
            dataset, batch_size=self.data.batch_size, shuffle=shuffle, num_workers=4, persistent_workers=True
        )

    @override
    def train_dataloader(self) -> DataLoader[tuple[torch.Tensor, torch.Tensor]]:
        return self._loader(self.train, shuffle=True)

    @override
    def val_dataloader(self) -> DataLoader[tuple[torch.Tensor, torch.Tensor]]:
        return self._loader(self.val, shuffle=False)

    @override
    def test_dataloader(self) -> DataLoader[tuple[torch.Tensor, torch.Tensor]]:
        return self._loader(self.test, shuffle=False)


def conv_block(in_channels: int, out_channels: int, norm: NormKind) -> nn.Sequential:
    """Two 3x3 convolutions with normalization."""
    layers: list[nn.Module] = []
    for cin in (in_channels, out_channels):
        layers += [
            nn.Conv2d(cin, out_channels, 3, padding=1, bias=False),
            nn.BatchNorm2d(out_channels)
            if norm is NormKind.BATCH
            else nn.GroupNorm(min(8, out_channels), out_channels),
            nn.ReLU(inplace=True),
        ]
    return nn.Sequential(*layers)


class UNet(nn.Module):
    """A UNet whose encoder (`down`) and decoder (`up` + `dec`) are separate parameter groups."""

    def __init__(self, cfg: ModelConfig) -> None:
        """Build `cfg.depth` down/up levels."""
        super().__init__()
        channels = [cfg.base_channels * 2**level for level in range(cfg.depth)]
        self.down = nn.ModuleList(
            conv_block(cin, cout, cfg.norm) for cin, cout in zip([3, *channels], channels, strict=False)
        )
        self.bottleneck = conv_block(channels[-1], channels[-1] * 2, cfg.norm)
        self.dropout = nn.Dropout2d(cfg.dropout)
        below = [channels[-1] * 2, *reversed(channels[1:])]
        self.up = nn.ModuleList(
            nn.ConvTranspose2d(cin, cout, 2, stride=2)
            for cin, cout in zip(below, reversed(channels), strict=True)
        )
        self.dec = nn.ModuleList(conv_block(2 * cout, cout, cfg.norm) for cout in reversed(channels))
        self.head = nn.Conv2d(channels[0], NUM_CLASSES, 1)

    @override
    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """Per-pixel class logits, NxCxHxW."""
        skips: list[torch.Tensor] = []
        for block in self.down:
            x = block(x)
            skips.append(x)
            x = nn.functional.max_pool2d(x, 2)
        x = self.dropout(self.bottleneck(x))
        for up, dec, skip in zip(self.up, self.dec, reversed(skips), strict=True):
            x = dec(torch.cat([skip, up(x)], dim=1))
        return self.head(x)


def mask_layer(mask: torch.Tensor) -> ImageMask:
    """A class mask, run-length encoded; the background has no color, so it is left clear."""
    return run_length_encode(mask.numpy(), colors=CLASS_COLORS, classnames=CLASS_NAMES)


def class_boxes(mask: torch.Tensor) -> list[tuple[ShapeClass, list[float]]]:
    """One tight `[x0, y0, x1, y1]` box per foreground class present in `mask`."""
    present = [c for c in FOREGROUND if (mask == c).any()]
    if not present:
        return []
    boxes = masks_to_boxes(torch.stack([mask == c for c in present]))
    return [(c, cast("list[float]", box.tolist())) for c, box in zip(present, boxes, strict=True)]


def box_shapes(mask: torch.Tensor, color: str) -> list[Shape]:
    """One box per foreground class in `mask`, in `color`, as fractions of the picture they go over."""
    height, width = mask.shape
    return [
        Box(
            x=x0 / width,
            y=y0 / height,
            width=(x1 - x0) / width,
            height=(y1 - y0) / height,
            label=shape.name.lower(),
            color=color,
        )
        for shape, (x0, y0, x1, y1) in class_boxes(mask)
    ]


def sample_iou(truth: torch.Tensor, pred: torch.Tensor) -> float:
    """Mean IoU over the foreground classes that appear in either mask."""
    ious = [
        ((truth == c) & (pred == c)).sum() / ((truth == c) | (pred == c)).sum()
        for c in FOREGROUND
        if ((truth == c) | (pred == c)).any()
    ]
    return float(torch.stack(ious).mean()) if ious else 1.0


def plot_class_iou(iou: dict[str, float]) -> go.Figure:
    """A bar per foreground class, in the color its masks are drawn in."""
    return go.Figure(
        go.Bar(
            x=list(iou),
            y=list(iou.values()),
            marker={"color": [CLASS_COLORS[int(ShapeClass[name.upper()])] for name in iou]},
        ),
        layout={
            "title": {"text": "Validation IoU per class"},
            "yaxis": {"title": {"text": "IoU"}, "range": [0, 1]},
        },
    )


def plot_class_balance(dataset: ShapesDataset) -> go.Figure:
    """The share of the first `CLASS_BALANCE_SAMPLES` images' pixels each class covers, the background included."""
    masks = torch.stack([dataset[i][1] for i in range(min(CLASS_BALANCE_SAMPLES, len(dataset)))])
    share = torch.bincount(masks.flatten(), minlength=NUM_CLASSES) / masks.numel()
    return go.Figure(
        go.Bar(
            x=[c.name.lower() for c in ShapeClass],
            y=share.tolist(),
            marker={"color": ["#888888", *(CLASS_COLORS[int(c)] for c in FOREGROUND)]},
        ),
        layout={
            "title": {"text": "Training pixels per class"},
            "yaxis": {"title": {"text": "share of pixels"}, "tickformat": ".0%"},
        },
    )


def sample_iou_template() -> PlotlyFigure:
    """
    A scatter of each validation sample's IoU against how much of it is foreground, its points left empty.

    The bindings say which `PlotlyData` column fills which attribute, so each epoch's data adds its points, colored by
    their epoch: whether training lifts small shapes as much as large ones shows as the colors climb.
    """
    template = go.Figure(
        go.Scatter(
            mode="markers",
            marker={
                "colorscale": "Viridis",
                "showscale": True,
                "colorbar": {"title": {"text": "epoch"}},
                "size": 6,
            },
            hovertemplate="foreground %{x:.1%}<br>IoU %{y:.2f}<br>epoch %{marker.color}<extra></extra>",
        ),
        layout={
            "title": {"text": "Validation IoU per sample"},
            "xaxis": {"title": {"text": "foreground share"}, "tickformat": ".0%"},
            "yaxis": {"title": {"text": "IoU"}, "range": [0, 1.02]},
        },
    )
    return PlotlyFigure.from_figure(
        template, SAMPLE_IOU_KEY, bind={0: {"x": "foreground", "y": "iou", "marker.color": "epoch"}}
    )


class ShapesSegmenter(pl.LightningModule):
    """UNet segmenter, with per-class IoU each validation epoch, and mask and box previews as often as `volume` says."""

    _confusion: torch.Tensor

    def __init__(
        self, model: ModelConfig, optim: OptimConfig, volume: Volume, encoder_lr_scale: float = 0.5
    ) -> None:
        """`save_hyperparameters` logs three args: two nested configs and a plain float."""
        super().__init__()
        self.save_hyperparameters(ignore=["volume"])
        self.volume = volume
        self.net = UNet(model)
        self.optim_cfg = optim
        self.encoder_lr_scale = encoder_lr_scale
        self.loss_fn = nn.CrossEntropyLoss()
        self.register_buffer(
            "_confusion", torch.zeros(NUM_CLASSES, NUM_CLASSES, dtype=torch.long), persistent=False
        )
        self._preview: tuple[torch.Tensor, torch.Tensor, torch.Tensor] | None = None

    @override
    def forward(self, images: torch.Tensor) -> torch.Tensor:
        """Per-pixel class logits."""
        return self.net(images)

    def _step(
        self, batch: tuple[torch.Tensor, torch.Tensor], stage: Stage
    ) -> tuple[torch.Tensor, torch.Tensor]:
        images, masks = batch
        logits = self(images)
        loss = self.loss_fn(logits, masks)
        preds = logits.argmax(dim=1)
        self.log(
            f"{stage}/loss",
            loss,
            on_step=stage is Stage.TRAIN,
            on_epoch=True,
            prog_bar=True,
            batch_size=images.size(0),
        )
        if stage is not Stage.TRAIN:
            self._confusion += torch.bincount(
                (masks * NUM_CLASSES + preds).flatten(), minlength=NUM_CLASSES**2
            ).view(NUM_CLASSES, NUM_CLASSES)
        return loss, preds

    @override
    def training_step(self, batch: tuple[torch.Tensor, torch.Tensor], batch_idx: int) -> torch.Tensor:
        loss, _ = self._step(batch, Stage.TRAIN)
        return loss

    @override
    def on_before_optimizer_step(self, optimizer: Optimizer) -> None:
        # The gradients themselves, not a norm of them: binned right here (one pass over each tensor), and only
        # the buckets are sent. `rank_zero_only`, so the other ranks of a multi-GPU run skip it entirely.
        if self.global_step % self.volume.grad_histogram_every_n_steps == 0:
            cast("DLBoardLogger", self.trainer.logger).log_histograms(
                {
                    f"grad/{name}": param.grad
                    for name, param in self.named_parameters()
                    if param.grad is not None and param.ndim > 1
                },
                step=self.global_step,
            )

    @override
    def on_validation_epoch_start(self) -> None:
        self._confusion.zero_()

    @override
    def validation_step(self, batch: tuple[torch.Tensor, torch.Tensor], batch_idx: int) -> None:
        _, preds = self._step(batch, Stage.VAL)
        if batch_idx == 0:  # the loader doesn't shuffle, so these are the same images every epoch
            # The first few images, for the previews, and the whole batch's masks, for the per-sample IoU scatter.
            images, masks = batch
            self._preview = (images[:PREVIEW_SAMPLES].cpu(), masks.cpu(), preds.cpu())

    def _log_segmentation_metrics(self, stage: Stage) -> dict[str, float]:
        """Log mIoU, dice and per-class IoU of the confusion matrix `_step` accumulated; returns per-class IoU."""
        tp = self._confusion.diag().float()
        total = self._confusion.sum(0) + self._confusion.sum(1)
        iou = tp / (total - tp).clamp(min=1)
        dice = 2 * tp / total.clamp(min=1)
        per_class = {c.name.lower(): float(iou[c]) for c in FOREGROUND}
        self.log_dict(
            {
                f"{stage}/mIoU": iou[1:].mean(),
                f"{stage}/dice": dice[1:].mean(),
                **{f"{stage}/iou/{name}": value for name, value in per_class.items()},
            }
        )
        return per_class

    @override
    def on_validation_epoch_end(self) -> None:
        per_class = self._log_segmentation_metrics(Stage.VAL)
        if self.trainer.sanity_checking or self._preview is None:
            return
        logger = cast("DLBoardLogger", self.trainer.logger)
        step, epoch = self.trainer.global_step, self.trainer.current_epoch
        images, masks, preds = self._preview
        artifacts: list[Image | Overlay | PlotlyFigure | PlotlyData] = []
        # Every few epochs, ending on the last: a slider over every epoch's pictures is too fine to scrub.
        previewing = (epoch + 1) % self.volume.preview_every_n_epochs == 0
        for i in range(len(images) if previewing else 0):
            rgb = (images[i].permute(1, 2, 0).numpy() * 255).astype(np.uint8)
            truth, pred = masks[i], preds[i]
            # DLBOARD GAP: one artifact per (key, step), so every preview image needs its own key.
            # The picture is plain; each mask is an `Overlay` the chart draws over it, so the picture stays a
            # small JPEG and the mask stays exact (run-length encoded). Tags are how numbers ride along with it.
            # Both masks are over the one picture, each named: the chart's legend shows and hides each, to compare.
            key = f"val/segmentation/sample_{i}"
            artifacts += [
                Image(
                    key=key,
                    image=rgb,
                    step=step,
                    format="jpg",
                    tags={"epoch": epoch, "iou": sample_iou(truth, pred)},
                ),
                *(
                    Overlay.over(key, name=name, shapes=[mask_layer(mask)], step=step)
                    for name, mask in (("truth", truth), ("prediction", pred))
                ),
            ]
            artifacts += [
                Image(
                    key=f"val/detection/sample_{i}",
                    image=rgb,
                    step=step,
                    format="jpg",
                    tags={
                        "epoch": epoch,
                        "true_boxes": len(class_boxes(truth)),
                        "predicted_boxes": len(class_boxes(pred)),
                    },
                ),
                # The boxes are overlays, drawn over the picture by the chart, not part of its pixels. There are two
                # of them over one picture, so each is named.
                Overlay.over(
                    f"val/detection/sample_{i}", name="truth", shapes=box_shapes(truth, "#00c853"), step=step
                ),
                Overlay.over(
                    f"val/detection/sample_{i}",
                    name="prediction",
                    shapes=box_shapes(pred, "#ff3b30"),
                    step=step,
                ),
            ]
        artifacts += [
            # A whole figure each epoch: the chart has a slider over them, and keeps a zoom as it moves.
            PlotlyFigure.from_figure(plot_class_iou(per_class), "val/iou_per_class", step=step),
            # Only the data, drawn into the template `main` logged: the chart adds each epoch's points to the last's.
            PlotlyData(
                template=SAMPLE_IOU_KEY,
                step=step,
                columns={
                    "foreground": (masks > 0).float().mean(dim=(1, 2)),
                    "iou": [sample_iou(truth, pred) for truth, pred in zip(masks, preds, strict=True)],
                    "epoch": np.full(len(masks), epoch),
                },
            ),
        ]
        logger.log_artifact(artifacts)

    @override
    def on_test_epoch_start(self) -> None:
        self._confusion.zero_()

    @override
    def test_step(self, batch: tuple[torch.Tensor, torch.Tensor], batch_idx: int) -> None:
        self._step(batch, Stage.TEST)

    @override
    def on_test_epoch_end(self) -> None:
        self._log_segmentation_metrics(Stage.TEST)

    @override
    def configure_optimizers(self) -> OptimizerLRScheduler:
        encoder = list(self.net.down.parameters())
        encoder_ids = {id(p) for p in encoder}
        decoder = [p for p in self.parameters() if id(p) not in encoder_ids]
        optimizer = AdamW(
            # Two parameter groups, so `LearningRateMonitor` logs `lr-AdamW/pg1` and `lr-AdamW/pg2`.
            [
                {"params": encoder, "lr": self.optim_cfg.lr * self.encoder_lr_scale},
                {"params": decoder, "lr": self.optim_cfg.lr},
            ],
            betas=self.optim_cfg.betas,
            weight_decay=self.optim_cfg.weight_decay,
        )
        total_steps = int(self.trainer.estimated_stepping_batches)
        schedule = self.optim_cfg.schedule

        def lr_scale(step: int) -> float:
            if step < schedule.warmup_steps:
                return (step + 1) / schedule.warmup_steps
            progress = min(1.0, (step - schedule.warmup_steps) / max(1, total_steps - schedule.warmup_steps))
            match schedule.kind:
                case ScheduleKind.COSINE:
                    return 0.5 * (1 + math.cos(math.pi * progress))
                case ScheduleKind.LINEAR:
                    return 1 - progress
                case ScheduleKind.CONSTANT:
                    return 1.0

        return {
            "optimizer": optimizer,
            "lr_scheduler": {"scheduler": LambdaLR(optimizer, lr_scale), "interval": "step"},
        }


def batch_size_of(batch: tuple[torch.Tensor, torch.Tensor]) -> int:
    """How many samples `ThroughputMonitor` should count in a batch."""
    return batch[0].size(0)


def main(volume: Volume) -> None:
    """Sample hyperparameters, train for `volume.max_epochs`, then evaluate on the test split."""
    LOGDIR.mkdir(exist_ok=True)
    logger = DLBoardLogger.from_names(
        "shape-segmentation",
        volume.experiment,
        "Synthetic shapes, to exercise nested hyperparameters, checkpoints and rich previews",
        log_model="all",
        server_url=os.environ.get(  # dlboard-ignore[forbid-string-ignore]
            "DLBOARD_SERVER_URL", "http://localhost:8050"
        ),
    )
    data = ShapesDataModule(DataConfig(), AugmentConfig())
    data.setup()
    # Logged once, at step 0: a figure drawn as it is, and the template the validation epochs stream data into.
    logger.log_artifact(
        [
            PlotlyFigure.from_figure(plot_class_balance(data.train), "data/class_balance"),
            sample_iou_template(),
        ]
    )
    model = ShapesSegmenter(
        ModelConfig(
            base_channels=random.choice([8, 16, 24, 32]),
            depth=random.choice([2, 3, 4]),
            dropout=round(random.uniform(0, 0.3), 2),
            norm=random.choice(list(NormKind)),
        ),
        OptimConfig(
            lr=10 ** random.uniform(-3.5, -2.5),
            weight_decay=10 ** random.uniform(-5, -3),
            schedule=ScheduleConfig(
                kind=random.choice(list(ScheduleKind)), warmup_steps=random.choice([50, 100, 200])
            ),
        ),
        volume,
        encoder_lr_scale=random.choice([0.25, 0.5, 1.0]),
    )

    trainer = pl.Trainer(
        max_epochs=volume.max_epochs,
        accelerator="auto",
        devices=1,
        precision="16-mixed" if torch.cuda.is_available() else "32-true",
        logger=logger,
        log_every_n_steps=volume.log_every_n_steps,
        enable_model_summary=False,
        callbacks=[
            LearningRateMonitor(logging_interval="step", log_momentum=True, log_weight_decay=True),
            ThroughputMonitor(batch_size_fn=batch_size_of, window_size=50),
            *([DeviceStatsMonitor(cpu_stats=True)] if volume.device_stats else []),
            ModelCheckpoint(
                dirpath=LOGDIR / f"run-{logger.run_id}" / "checkpoints",
                filename="epoch{epoch:02d}-step{step}",
                auto_insert_metric_name=False,
                monitor="val/mIoU",
                mode="max",
                save_top_k=3,
                save_last=True,
            ),
        ],
    )
    trainer.fit(model, datamodule=data)
    trainer.test(model, datamodule=data)


if __name__ == "__main__":
    main(TOUR)
