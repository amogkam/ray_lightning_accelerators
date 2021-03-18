from typing import Optional, List

import torch
from pytorch_lightning.plugins import Plugin
from torch.utils.data import Dataset

import pytorch_lightning as pl
from pytorch_lightning import LightningModule, Callback, Trainer, \
    LightningDataModule


class RandomDataset(Dataset):
    def __init__(self, size: int, length: int):
        self.len = length
        self.data = torch.randn(length, size)

    def __getitem__(self, index: int):
        return self.data[index]

    def __len__(self):
        return self.len


class BoringModel(LightningModule):
    def __init__(self):
        super().__init__()
        self.layer = torch.nn.Linear(32, 2)
        self.val_epoch = 0

    def forward(self, x):
        return self.layer(x)

    def loss(self, batch, prediction):
        # Arbitrary loss to have a loss that updates the model weights
        # during `Trainer.fit` calls
        return torch.nn.functional.mse_loss(prediction,
                                            torch.ones_like(prediction))

    def step(self, x):
        x = self(x)
        out = torch.nn.functional.mse_loss(x, torch.ones_like(x))
        return out

    def training_step(self, batch, batch_idx):
        output = self.layer(batch)
        loss = self.loss(batch, output)
        return {"loss": loss}

    def training_step_end(self, training_step_outputs):
        return training_step_outputs

    def training_epoch_end(self, outputs) -> None:
        torch.stack([x["loss"] for x in outputs]).mean()

    def validation_step(self, batch, batch_idx):
        self.layer(batch)
        loss = torch.tensor(1.0)
        self.log("val_loss", loss)
        return {"x": loss}

    def validation_epoch_end(self, outputs) -> None:
        torch.stack([x["x"] for x in outputs]).mean()
        self.val_epoch += 1

    def test_step(self, batch, batch_idx):
        output = self.layer(batch)
        loss = self.loss(batch, output)
        return {"y": loss}

    def test_epoch_end(self, outputs) -> None:
        torch.stack([x["y"] for x in outputs]).mean()

    def configure_optimizers(self):
        optimizer = torch.optim.SGD(self.layer.parameters(), lr=0.1)
        lr_scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=1)
        return [optimizer], [lr_scheduler]

    def train_dataloader(self):
        return torch.utils.data.DataLoader(RandomDataset(32, 64))

    def val_dataloader(self):
        return torch.utils.data.DataLoader(RandomDataset(32, 64))

    def test_dataloader(self):
        return torch.utils.data.DataLoader(RandomDataset(32, 64))

    def on_save_checkpoint(self, checkpoint):
        checkpoint["val_epoch"] = self.val_epoch

    def on_load_checkpoint(self, checkpoint) -> None:
        self.val_epoch = checkpoint["val_epoch"]


def get_trainer(dir,
                plugins: List[Plugin],
                use_gpu: bool = False,
                max_epochs: int = 1,
                limit_train_batches: int = 10,
                limit_val_batches: int = 10,
                progress_bar_refresh_rate: int = 0,
                callbacks: Optional[List[Callback]] = None,
                checkpoint_callback: bool = True) -> Trainer:
    """Returns a Pytorch Lightning Trainer with the provided arguments."""
    callbacks = [] if not callbacks else callbacks
    trainer = pl.Trainer(
        default_root_dir=dir,
        gpus=1 if use_gpu else 0,
        max_epochs=max_epochs,
        limit_train_batches=limit_train_batches,
        limit_val_batches=limit_val_batches,
        progress_bar_refresh_rate=progress_bar_refresh_rate,
        checkpoint_callback=checkpoint_callback,
        callbacks=callbacks,
        plugins=plugins)
    return trainer


def train_test(trainer: Trainer, model: LightningModule):
    """Checks if training the provided model updates its weights."""
    initial_values = torch.tensor(
        [torch.sum(torch.abs(x)) for x in model.parameters()])
    result = trainer.fit(model)
    post_train_values = torch.tensor(
        [torch.sum(torch.abs(x)) for x in model.parameters()])
    assert result == 1, "trainer failed"
    # Check that the model is actually changed post-training.
    assert torch.norm(initial_values - post_train_values) > 0.1


def load_test(trainer: Trainer, model: LightningModule):
    """Checks if the model checkpoint can be loaded."""
    trainer.fit(model)
    trained_model = BoringModel.load_from_checkpoint(
        trainer.checkpoint_callback.best_model_path)
    assert trained_model is not None, "loading model failed"


def predict_test(trainer: Trainer, model: LightningModule,
                 dm: LightningDataModule):
    """Checks if the trained model has high accuracy on the test set."""
    trainer.fit(model, datamodule=dm)
    dm.setup(stage="test")
    test_loader = dm.test_dataloader()
    acc = pl.metrics.Accuracy()
    for batch in test_loader:
        x, y = batch
        with torch.no_grad():
            y_hat = model(x)
        y_hat = y_hat.cpu()
        acc.update(y_hat, y)
    average_acc = acc.compute()
    assert average_acc >= 0.5, f"This model is expected to get > {0.5} in " \
                               f"test set (it got {average_acc})"
