"""
A deliberately heavy Lightning run, to put dlboard's UI through its paces with nothing but stock APIs.

A small UNet learns to segment synthetic shapes (circles, squares, triangles on a noisy background).
Nothing is downloaded, and it trains in a few minutes on a GPU (and a bit longer on a CPU). What it
exercises, all through ordinary Lightning/dlboard calls:

- nested hyperparameters: pydantic models, a pydantic dataclass, a stdlib dataclass and plain args,
  flattened by the logger to `model/base_channels`, `optim/schedule/kind`, ...
- 25 epochs with step- and epoch-level metrics, per-class IoU, and ~dozens of per-parameter gradient norms
- Lightning's `LearningRateMonitor`, `ThroughputMonitor` and `DeviceStatsMonitor`
- `ModelCheckpoint` with `log_model="all"`, so every checkpoint is uploaded as it is saved
- per-epoch prediction previews of segmentation masks and bounding boxes, plus a matplotlib figure

Run it a few times: each run samples fresh hyperparameters, so the run-comparison views have something
to compare. What follows is what dlboard can't do yet, found by running this -- the backlog this example
exists to produce. Where the script itself works around one, a `# DLBOARD GAP:` comment says so:

1. No mask/box artifact. Overlays and boxes are composited into a plain image client-side, so the UI can't
   toggle classes, change opacity or compare ground truth against prediction.
2. One artifact per (run, key, step), so N previews need N keys (`sample_0`, `sample_1`, ...) rather than
   one batch rendered as a gallery.
3. Artifact tags are `dict[str, str]`, so a numeric score has to be stringified to be shown as a caption,
   and each caller formats it differently (#179).
4. A run has no status: Lightning's `finalize("success" | "failed")` is never recorded, so a crashed run
   looks the same as a finished one, and there is no duration (#177).
5. Nothing to skip or collapse a noisy group: `DeviceStatsMonitor` alone adds ~500 series, so auto-generate
   offers 886 charts, and its panels sort first, ahead of `train` and `val`.
6. Auto-generating that many charts is slow, and so is switching Prefix/Suffix in its dialog (#168, #167).
7. Changing charts-per-row in a big Grid panel blanks the UI (#169).
8. Suggest charts is one add button per key: no select-all, filter or sort (#173).
"""

import math
import random
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import cast, override

import numpy as np
import PIL.Image
import PIL.ImageDraw
import pydantic
import pydantic.dataclasses
import pytorch_lightning as pl
import torch
from matplotlib.figure import Figure
from pydantic import BaseModel, Field
from pytorch_lightning.callbacks import (
    DeviceStatsMonitor,
    LearningRateMonitor,
    ModelCheckpoint,
    ThroughputMonitor,
)
from pytorch_lightning.utilities import grad_norm
from pytorch_lightning.utilities.types import OptimizerLRScheduler
from torch import nn
from torch.optim import AdamW, Optimizer
from torch.optim.lr_scheduler import LambdaLR
from torch.utils.data import DataLoader, Dataset
from torchvision.ops import masks_to_boxes

from dlboard.client.artifacts.image import Image
from dlboard.client.dlboard_logger import DLBoardLogger

LOGDIR = Path("./lightning-logs")
MAX_EPOCHS = 25
GRAD_NORM_EVERY_N_STEPS = 10
PREVIEW_SAMPLES = 4
PREVIEW_SCALE = 3
"""How many fixed validation images get a segmentation and a detection preview each epoch, and how much they're enlarged."""
CLASS_COLORS = np.array([[0, 0, 0], [230, 60, 60], [60, 200, 90], [70, 110, 240]], dtype=np.uint8)
"""RGB per `ShapeClass`, indexed by class id."""


class ShapeClass(IntEnum):
    """A pixel's class: the shape drawn on it, or the background."""

    BACKGROUND = 0
    CIRCLE = 1
    SQUARE = 2
    TRIANGLE = 3


FOREGROUND = tuple(c for c in ShapeClass if c is not ShapeClass.BACKGROUND)
NUM_CLASSES = len(ShapeClass)


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
        return torch.from_numpy(image), torch.from_numpy(mask)  # pyright: ignore[reportUnknownMemberType]


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


def overlay(image: np.ndarray, mask: np.ndarray, alpha: float = 0.55) -> np.ndarray:
    """Blend each foreground class's color over `image` (HxWx3 uint8)."""
    blended = (image * (1 - alpha) + CLASS_COLORS[mask] * alpha).astype(np.uint8)
    return np.where((mask > 0)[..., None], blended, image)


def class_boxes(mask: torch.Tensor) -> list[tuple[ShapeClass, list[float]]]:
    """One tight `[x0, y0, x1, y1]` box per foreground class present in `mask`."""
    present = [c for c in FOREGROUND if (mask == c).any()]
    if not present:
        return []
    boxes = masks_to_boxes(torch.stack([mask == c for c in present]))
    return [(c, cast("list[float]", box.tolist())) for c, box in zip(present, boxes, strict=True)]  # pyright: ignore[reportUnknownMemberType]


def draw_boxes(image: np.ndarray, truth: torch.Tensor, pred: torch.Tensor) -> np.ndarray:
    """`image` (HxWx3 uint8) enlarged, with ground-truth boxes in green and predicted ones in red."""
    canvas = PIL.Image.fromarray(image).resize(
        (image.shape[1] * PREVIEW_SCALE, image.shape[0] * PREVIEW_SCALE)
    )
    draw = PIL.ImageDraw.Draw(canvas)
    for color, mask in (("lime", truth), ("red", pred)):
        for shape, (x0, y0, x1, y1) in class_boxes(mask):
            draw.rectangle(
                (x0 * PREVIEW_SCALE, y0 * PREVIEW_SCALE, x1 * PREVIEW_SCALE, y1 * PREVIEW_SCALE),
                outline=color,
                width=2,
            )
            draw.text((x0 * PREVIEW_SCALE + 3, y0 * PREVIEW_SCALE + 3), shape.name.lower(), fill=color)
    return np.asarray(canvas)


def sample_iou(truth: torch.Tensor, pred: torch.Tensor) -> float:
    """Mean IoU over the foreground classes that appear in either mask."""
    ious = [
        ((truth == c) & (pred == c)).sum() / ((truth == c) | (pred == c)).sum()
        for c in FOREGROUND
        if ((truth == c) | (pred == c)).any()
    ]
    return float(torch.stack(ious).mean()) if ious else 1.0


def plot_class_iou(iou: dict[str, float]) -> Figure:
    """A bar per foreground class."""
    fig = Figure(figsize=(5, 3), layout="constrained")
    ax = fig.subplots()
    ax.bar(list(iou), list(iou.values()))  # pyright: ignore[reportUnknownMemberType]
    ax.set(ylim=(0, 1), ylabel="IoU", title="Validation IoU per class")
    return fig


class ShapesSegmenter(pl.LightningModule):
    """UNet segmenter, with per-class IoU plus mask and box previews logged each validation epoch."""

    _confusion: torch.Tensor

    def __init__(self, model: ModelConfig, optim: OptimConfig, encoder_lr_scale: float = 0.5) -> None:
        """`save_hyperparameters` logs all three args: two nested configs and a plain float."""
        super().__init__()
        self.save_hyperparameters()
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
        # One `grad_2.0_norm/<param name>` key per parameter: a lot of series, grouped by their prefix.
        if self.global_step % GRAD_NORM_EVERY_N_STEPS == 0:
            self.log_dict(grad_norm(self, norm_type=2))

    @override
    def on_validation_epoch_start(self) -> None:
        self._confusion.zero_()

    @override
    def validation_step(self, batch: tuple[torch.Tensor, torch.Tensor], batch_idx: int) -> None:
        _, preds = self._step(batch, Stage.VAL)
        if batch_idx == 0:  # the loader doesn't shuffle, so these are the same images every epoch
            images, masks = batch
            self._preview = tuple(t[:PREVIEW_SAMPLES].cpu() for t in (images, masks, preds))  # pyright: ignore[reportAttributeAccessIssue]

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
        step, epoch = self.trainer.global_step, str(self.trainer.current_epoch)
        images, masks, preds = self._preview
        artifacts: list[Image] = []
        for i in range(len(images)):
            rgb = (images[i].permute(1, 2, 0).numpy() * 255).astype(np.uint8)
            truth, pred = masks[i], preds[i]
            # DLBOARD GAP: no mask/box artifact (see the module docstring) -- composite them into a
            # picture ourselves. Tags are the only way to attach numbers, and must be strings.
            side_by_side = np.concatenate(
                [rgb, overlay(rgb, truth.numpy()), overlay(rgb, pred.numpy())], axis=1
            )
            # DLBOARD GAP: one artifact per (key, step), so every preview image needs its own key.
            artifacts += [
                Image(
                    key=f"val/segmentation/sample_{i}",
                    image=side_by_side.repeat(PREVIEW_SCALE, axis=0).repeat(PREVIEW_SCALE, axis=1),
                    step=step,
                    tags={"epoch": epoch, "iou": f"{sample_iou(truth, pred):.3f}"},
                ),
                Image(
                    key=f"val/detection/sample_{i}",
                    image=draw_boxes(rgb, truth, pred),
                    step=step,
                    tags={
                        "epoch": epoch,
                        "boxes": f"{len(class_boxes(truth))} true / {len(class_boxes(pred))} predicted",
                    },
                ),
            ]
        logger.log_artifact(artifacts)
        logger.log_figure(plot_class_iou(per_class), "val/iou_per_class", step=step)

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


def main() -> None:
    """Sample hyperparameters, train for `MAX_EPOCHS`, then evaluate on the test split."""
    LOGDIR.mkdir(exist_ok=True)
    logger = DLBoardLogger.from_names(
        "shape-segmentation",
        "unet-advanced",
        "Synthetic shapes, to exercise nested hyperparameters, checkpoints and rich previews",
        log_model="all",
    )
    data = ShapesDataModule(DataConfig(), AugmentConfig())
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
        encoder_lr_scale=random.choice([0.25, 0.5, 1.0]),
    )

    trainer = pl.Trainer(
        max_epochs=MAX_EPOCHS,
        accelerator="auto",
        devices=1,
        precision="16-mixed" if torch.cuda.is_available() else "32-true",
        logger=logger,
        log_every_n_steps=5,
        enable_model_summary=False,
        callbacks=[
            LearningRateMonitor(logging_interval="step", log_momentum=True, log_weight_decay=True),
            ThroughputMonitor(batch_size_fn=batch_size_of, window_size=50),
            DeviceStatsMonitor(cpu_stats=True),
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
    # DLBOARD GAP 4: the status Lightning reports when this finishes (or crashes) is dropped.
    trainer.fit(model, datamodule=data)
    trainer.test(model, datamodule=data)


if __name__ == "__main__":
    main()
