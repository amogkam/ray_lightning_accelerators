import math
from typing import Dict, List, Union

import os

from pytorch_lightning.utilities.cloud_io import atomic_save
from pytorch_lightning import Trainer, LightningModule

from ray_lightning.session import put_queue, get_actor_rank
from ray_lightning.util import Unavailable

try:
    from ray import tune
    from ray.tune.integration.pytorch_lightning import TuneCallback
    from ray.tune import is_session_enabled
    TUNE_INSTALLED = True
except ImportError:
    tune = None
    TuneCallback = object

    def is_session_enabled():
        return False

    TUNE_INSTALLED = False

def get_tune_ddp_resources(num_workers: int = 1, cpus_per_worker: int = 1,
                           use_gpu:
bool = False) -> Dict[str, int]:
    """Returns the resources dictionary to use for Ray Tune."""
    resources = {}
    resources["cpu"] = 1
    resources["extra_cpu"] = num_workers * cpus_per_worker
    if use_gpu:
        total_worker_gpus = num_workers * 0.99
        resources["extra_gpu"] = total_worker_gpus
        resources["gpu"] = math.ceil(total_worker_gpus) - total_worker_gpus


if TUNE_INSTALLED:

    class TuneReportCallback(TuneCallback):
        """Distributed PyTorch Lightning to Ray Tune reporting callback

            Reports metrics to Ray Tune, specifically when training is done
            remotely with Ray via the accelerators in this library.

            Args:
                metrics (str|list|dict): Metrics to report to Tune.
                    If this is a list, each item describes the metric key
                    reported to PyTorch Lightning, and it will reported
                    under the same name to Tune. If this is a dict, each key
                    will be the name reported to Tune and the respective
                    value will be the metric key reported to PyTorch Lightning.
                on (str|list): When to trigger checkpoint creations.
                    Must be one of the PyTorch Lightning event hooks (less
                    the ``on_``), e.g. "batch_start", or "train_end".
                    Defaults to "validation_end".

            Example:

            .. code-block:: python

                import pytorch_lightning as pl
                from ray_lightning import RayAccelerator
                from ray_lightning.tune import TuneReportCallback

                # Create accelerator
                ray_accelerator = RayAccelerator(num_workers=4, use_gpu=True)

                # Report loss and accuracy to Tune after each validation epoch:
                trainer = pl.Trainer(accelerator=ray_accelerator, callbacks=[
                    TuneReportCallback(["val_loss", "val_acc"],
                        on="validation_end")])

                # Same as above, but report as `loss` and `mean_accuracy`:
                trainer = pl.Trainer(accelerator=ray_accelerator, callbacks=[
                    TuneReportCallback(
                        {"loss": "val_loss", "mean_accuracy": "val_acc"},
                        on="validation_end")])

            """

        def __init__(
                self,
                metrics: Union[None, str, List[str], Dict[str, str]] = None,
                on: Union[str, List[str]] = "validation_end"):
            super(TuneReportCallback, self).__init__(on)
            if isinstance(metrics, str):
                metrics = [metrics]
            self._metrics = metrics

        def _get_report_dict(self, trainer: Trainer,
                             pl_module: LightningModule):
            # Don't report if just doing initial validation sanity checks.
            if trainer.running_sanity_check:
                return
            if not self._metrics:
                report_dict = {
                    k: v.item()
                    for k, v in trainer.callback_metrics.items()
                }
            else:
                report_dict = {}
                for key in self._metrics:
                    if isinstance(self._metrics, dict):
                        metric = self._metrics[key]
                    else:
                        metric = key
                    report_dict[key] = trainer.callback_metrics[metric].item()
            return report_dict

        def _handle(self, trainer: Trainer, pl_module: LightningModule):
            if get_actor_rank() == 0:
                report_dict = self._get_report_dict(trainer, pl_module)
                if report_dict is not None:
                    put_queue(lambda: tune.report(**report_dict))

    class _TuneCheckpointCallback(TuneCallback):
        """Distributed PyTorch Lightning to Ray Tune checkpoint callback

            Saves checkpoints after each validation step. To be used
            specifically with the accelerators in this library.

            Checkpoint are currently not registered if no ``tune.report()``
            call is made afterwards. Consider using
            ``TuneReportCheckpointCallback`` instead.

            Args:
                filename (str): Filename of the checkpoint within the
                    checkpoint directory. Defaults to "checkpoint".
                on (str|list): When to trigger checkpoint creations.
                    Must be one of the PyTorch Lightning event hooks (less
                    the ``on_``), e.g. "batch_start", or "train_end".
                    Defaults to "validation_end".
        """

        def __init__(self,
                     filename: str = "checkpoint",
                     on: Union[str, List[str]] = "validation_end"):
            super(_TuneCheckpointCallback, self).__init__(on)
            self._filename = filename

        @staticmethod
        def _create_checkpoint(checkpoint_dict: dict, global_step: int,
                               filename: str):
            with tune.checkpoint_dir(step=global_step) as checkpoint_dir:
                file_path = os.path.join(checkpoint_dir, filename)
                atomic_save(checkpoint_dict, file_path)

        def _handle(self, trainer: Trainer, pl_module: LightningModule):
            if trainer.running_sanity_check:
                return
            checkpoint_dict = trainer.checkpoint_connector.dump_checkpoint()
            global_step = trainer.global_step
            if get_actor_rank() == 0:
                put_queue(lambda: self._create_checkpoint(
                    checkpoint_dict, global_step, self._filename))

    class TuneReportCheckpointCallback(TuneCallback):
        """PyTorch Lightning to Tune reporting and checkpointing callback.

            Saves checkpoints after each validation step. Also reports metrics
            to Tune, which is needed for checkpoint registration. To be used
            specifically with the accelerators in this library.

            Args:
                metrics (str|list|dict): Metrics to report to Tune.
                    If this is a list, each item describes the metric key
                    reported to PyTorch Lightning, and it will reported
                    under the same name to Tune. If this is a dict, each key
                    will be the name reported to Tune and the respective
                    value will be the metric key reported to PyTorch Lightning.
                filename (str): Filename of the checkpoint within the
                    checkpoint directory. Defaults to "checkpoint".
                on (str|list): When to trigger checkpoint creations. Must be
                    one of the PyTorch Lightning event hooks (less the
                    ``on_``), e.g. "batch_start", or "train_end". Defaults
                    to "validation_end".


            Example:

            .. code-block:: python

                import pytorch_lightning as pl
                from ray_lightning import RayAccelerator
                from ray_lightning.tune import TuneReportCheckpointCallback.

                # Create the Ray accelerator.
                ray_accelerator = RayAccelerator()

                # Save checkpoint after each training batch and after each
                # validation epoch.
                trainer = pl.Trainer(accelerator=ray_accelerator, callbacks=[
                    TuneReportCheckpointCallback(
                        metrics={"loss": "val_loss",
                                "mean_accuracy": "val_acc"},
                        filename="trainer.ckpt", on="validation_end")])


            """

        def __init__(
                self,
                metrics: Union[None, str, List[str], Dict[str, str]] = None,
                filename: str = "checkpoint",
                on: Union[str, List[str]] = "validation_end"):
            super(TuneReportCheckpointCallback, self).__init__(on)
            self._checkpoint = _TuneCheckpointCallback(filename, on)
            self._report = TuneReportCallback(metrics, on)

        def _handle(self, trainer: Trainer, pl_module: LightningModule):
            self._checkpoint._handle(trainer, pl_module)
            self._report._handle(trainer, pl_module)

else:
    # If Tune is not installed.
    TuneReportCallback = Unavailable
    TuneReportCheckpointCallback = Unavailable
