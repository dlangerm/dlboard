"""training loop."""

from pathlib import Path
from random import random
from typing import cast, override

import pytorch_lightning as pl
import torch
from torch import nn
from torch.utils.data import DataLoader, random_split
from torchvision import datasets, transforms

from dltrack.client import DLTrackLogger, Image

LOGDIR = Path("./lightning-logs")


class MnistMLP(pl.LightningModule):
    """Tiny MLP for MNIST classification with a few easy-to-extend metrics."""

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
        self.log_extra_artifacts(batch, preds, targets, stage=stage)

        return loss, acc

    @override
    def training_step(self, batch: tuple[torch.Tensor, torch.Tensor], batch_idx: int) -> torch.Tensor:
        loss, _ = self._shared_step(batch, "train")
        return loss

    @override
    def validation_step(self, batch: tuple[torch.Tensor, torch.Tensor], batch_idx: int) -> None:
        self._shared_step(batch, "val")

    @override
    def test_step(self, batch: tuple[torch.Tensor, torch.Tensor], batch_idx: int) -> None:
        self._shared_step(batch, "test")

    @override
    def configure_optimizers(self) -> torch.optim.Optimizer:
        return torch.optim.Adam(self.parameters(), lr=self._learning_rate)

    def log_extra_artifacts(
        self,
        batch: tuple[torch.Tensor, torch.Tensor],
        preds: torch.Tensor,
        targets: torch.Tensor,
        stage: str,
    ) -> None:
        """Hook for future image/table logging without cluttering the main training loop."""
        inputs, _ = batch
        assert self.trainer.logger is not None
        lg = cast("DLTrackLogger", self.trainer.logger)
        inputs = inputs.permute(0, 2, 3, 1)  # channels last
        lg.log_image(
            f"{stage}/img",
            [
                Image(
                    image=(inputs[0].squeeze(-1) * 256).to(torch.uint8),
                    tags={"pred": str(preds[0].item()), "target": str(targets[0].item())},
                    step=self.trainer.global_step,
                )
            ],
        )


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


def main() -> None:
    """Entrypoint for training."""
    LOGDIR.mkdir(exist_ok=True)
    logger = DLTrackLogger(project_id=1, experiment_id=1)
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
