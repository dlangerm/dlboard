"""
Training loop, doubling as a demo of dlboard's image-logging path under realistic image weight.

Per-batch debug crops (the old version's `log_extra_artifacts`) barely exercise the upload path --
they're tiny and infrequent. This logs two heavier things instead: a full-resolution confusion
matrix once per validation epoch, and a large tiled mosaic once at startup. The actual performance
guarantee for that workload (colocated client/server should be fast; degradation beyond that should
come from the network, not dlboard) is asserted in `dlboard/client/tests/dlboard_logger_perf_test.py`,
not here -- this script is just meant to look like something a real training run would log.
"""

from pathlib import Path
from random import random
from typing import cast, override

import numpy as np
import pytorch_lightning as pl
import torch
from PIL import Image as PILImage
from PIL import ImageDraw
from torch import nn
from torch.utils.data import DataLoader, random_split
from torchvision import datasets, transforms

from dlboard.client.artifacts import image
from dlboard.client.dlboard_logger import DLBoardLogger

LOGDIR = Path("./lightning-logs")
NUM_CLASSES = 10
MOSAIC_GRID = 16
MOSAIC_CELL_PX = 128
CONFUSION_MATRIX_CELL_PX = 80
_LIGHT_CELL_THRESHOLD = 127
"""Above this grayscale intensity, a cell is light enough that its count label needs dark text."""


def render_confusion_matrix(counts: np.ndarray, cell_px: int = CONFUSION_MATRIX_CELL_PX) -> np.ndarray:
    """Render a count matrix as a high-resolution grayscale heatmap, darker cell = more counts."""
    n = counts.shape[0]
    size = n * cell_px
    img = PILImage.new("L", (size, size), color=255)
    draw = ImageDraw.Draw(img)
    vmax = max(int(counts.max()), 1)
    for row in range(n):
        for col in range(n):
            intensity = 255 - round(255 * int(counts[row, col]) / vmax)
            x0, y0 = col * cell_px, row * cell_px
            draw.rectangle([x0, y0, x0 + cell_px, y0 + cell_px], fill=intensity)
            fill = 0 if intensity > _LIGHT_CELL_THRESHOLD else 255
            draw.text((x0 + 4, y0 + 4), str(int(counts[row, col])), fill=fill)  # pyright: ignore[reportUnknownMemberType]
    for k in range(n + 1):
        draw.line([(0, k * cell_px), (size, k * cell_px)], fill=128)
        draw.line([(k * cell_px, 0), (k * cell_px, size)], fill=128)
    return np.array(img)


def build_mosaic(
    images: torch.Tensor, grid: int = MOSAIC_GRID, cell_px: int = MOSAIC_CELL_PX
) -> torch.Tensor:
    """Tile `grid * grid` images (each in [0, 1], HxW) into one large upscaled mosaic."""
    tiles = images[: grid * grid]
    canvas = torch.zeros((grid * cell_px, grid * cell_px), dtype=torch.uint8)
    for idx in range(len(tiles)):
        row, col = divmod(idx, grid)
        cell = torch.nn.functional.interpolate(
            tiles[idx].view(1, 1, *tiles[idx].shape), size=(cell_px, cell_px), mode="nearest"
        )
        canvas[row * cell_px : (row + 1) * cell_px, col * cell_px : (col + 1) * cell_px] = (
            cell.squeeze().clamp(0, 1) * 255
        ).to(torch.uint8)
    return canvas


class MnistMLP(pl.LightningModule):
    """Tiny MLP for MNIST classification, with a per-epoch validation confusion matrix."""

    def __init__(self, hidden_size: int = 128, learning_rate: float = 1e-3) -> None:
        """The mlp model."""
        super().__init__()
        self.save_hyperparameters()
        self.model = nn.Sequential(
            nn.Flatten(),
            nn.Linear(28 * 28, hidden_size),
            nn.ReLU(),
            nn.Linear(hidden_size, 10),
        )
        self.loss_fn = nn.CrossEntropyLoss()
        self._learning_rate = learning_rate
        self._val_confusion = np.zeros((NUM_CLASSES, NUM_CLASSES), dtype=np.int64)

    def forward(self, batch: torch.Tensor) -> torch.Tensor:
        """Forward."""
        return self.model(batch)

    def _shared_step(
        self, batch: tuple[torch.Tensor, torch.Tensor], stage: str
    ) -> tuple[torch.Tensor, torch.Tensor]:
        inputs, targets = batch
        logits = self(inputs)
        loss = self.loss_fn(logits, targets)
        preds = logits.argmax(dim=1)
        acc = (preds == targets).float().mean()

        self.log(
            f"{stage}/loss",
            loss,
            on_step=True,
            on_epoch=True,
            prog_bar=True,
            logger=True,
            batch_size=inputs.size(0),
        )
        self.log(
            f"{stage}/acc",
            acc,
            on_step=True,
            on_epoch=True,
            prog_bar=True,
            logger=True,
            batch_size=inputs.size(0),
        )
        if stage == "val":
            np.add.at(self._val_confusion, (targets.cpu().numpy(), preds.cpu().numpy()), 1)

        return loss, acc

    @override
    def training_step(self, batch: tuple[torch.Tensor, torch.Tensor], batch_idx: int) -> torch.Tensor:
        loss, _ = self._shared_step(batch, "train")
        return loss

    @override
    def on_validation_epoch_start(self) -> None:
        self._val_confusion[:] = 0

    @override
    def validation_step(self, batch: tuple[torch.Tensor, torch.Tensor], batch_idx: int) -> None:
        self._shared_step(batch, "val")

    @override
    def on_validation_epoch_end(self) -> None:
        assert self.trainer.logger is not None
        lg = cast("DLBoardLogger", self.trainer.logger)
        lg.log_artifact(
            [
                image.Image(
                    key="val/confusion_matrix",
                    image=render_confusion_matrix(self._val_confusion),
                    step=self.trainer.global_step,
                )
            ]
        )

    @override
    def test_step(self, batch: tuple[torch.Tensor, torch.Tensor], batch_idx: int) -> None:
        self._shared_step(batch, "test")

    @override
    def configure_optimizers(self) -> torch.optim.Optimizer:
        return torch.optim.Adam(self.parameters(), lr=self._learning_rate)


class MnistDataModule(pl.LightningDataModule):
    """Data module for mnist."""

    def __init__(self, data_dir: str | Path = "./data", batch_size: int = 128) -> None:
        """Bdsafsd."""
        super().__init__()
        self.data_dir = Path(data_dir)
        self.batch_size = batch_size
        self.transform = transforms.Compose(
            [transforms.ToTensor(), transforms.Normalize((0.1307,), (0.3081,))]
        )

    @override
    def setup(self, stage: str | None = None) -> None:
        full_train = datasets.MNIST(root=self.data_dir, train=True, download=True, transform=self.transform)
        full_test = datasets.MNIST(root=self.data_dir, train=False, download=True, transform=self.transform)

        train_size = int(len(full_train) * 0.9)
        val_size = len(full_train) - train_size
        self.train_dataset, self.val_dataset = random_split(full_train, [train_size, val_size])  # pyright: ignore[reportUnknownMemberType]
        self.test_dataset = full_test

    @override
    def train_dataloader(self) -> DataLoader[datasets.MNIST]:
        return DataLoader[datasets.MNIST](
            self.train_dataset,  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
            batch_size=self.batch_size,
            shuffle=True,
            num_workers=7,
        )

    @override
    def val_dataloader(self) -> DataLoader[datasets.MNIST]:
        return DataLoader[datasets.MNIST](
            self.val_dataset,  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
            batch_size=self.batch_size,
            shuffle=False,
            num_workers=2,
        )

    @override
    def test_dataloader(self) -> DataLoader[datasets.MNIST]:
        return DataLoader[datasets.MNIST](
            self.test_dataset,
            batch_size=self.batch_size,
            shuffle=False,
            num_workers=0,
        )


def log_startup_mosaic(logger: DLBoardLogger, data_dir: str | Path) -> None:
    """Log one large tiled mosaic of raw (unnormalized) digits -- a heavier one-shot upload."""
    raw = datasets.MNIST(root=data_dir, train=False, download=True, transform=transforms.ToTensor())
    sample = torch.stack([raw[i][0].squeeze(0) for i in range(MOSAIC_GRID * MOSAIC_GRID)])
    logger.log_artifact([image.Image(key="dataset/mosaic", image=build_mosaic(sample), step=0)])


def main() -> None:
    """Entrypoint for training."""
    LOGDIR.mkdir(exist_ok=True)
    logger = DLBoardLogger.from_names(
        "character-classification",
        "mnist",
        "Classifying characters",
    )
    log_startup_mosaic(logger, data_dir="./.data")

    data = MnistDataModule(data_dir="./.data", batch_size=128)
    hidden_size = max(16, int((random() * 2048)))
    lr = random() * 1e-3
    model = MnistMLP(hidden_size=hidden_size, learning_rate=lr)

    trainer = pl.Trainer(
        max_epochs=5,
        devices=1,
        logger=logger,
        log_every_n_steps=1,
        enable_checkpointing=False,
        enable_model_summary=False,
    )

    trainer.fit(model, datamodule=data)
    trainer.test(model, datamodule=data)


if __name__ == "__main__":
    main()
