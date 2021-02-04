import pytest
import ray
from ray import tune

from ray_lightning import RayAccelerator
from ray_lightning.tests.utils import BoringModel, get_trainer
from ray_lightning.tune import TuneReportCallback


@pytest.fixture
def ray_start_4_cpus():
    address_info = ray.init(num_cpus=4)
    yield address_info
    ray.shutdown()

def train_func(dir, accelerator, use_gpu=False, callbacks=None):
    def _inner_train(config):
        model = BoringModel()
        trainer = get_trainer(dir, use_gpu=use_gpu,
                              callbacks=callbacks, accelerator=accelerator,
                              **config)
        trainer.fit(model)
    return _inner_train


def test_tune_iteration_ddp(tmpdir, ray_start_4_cpus):
    """Tests whether RayAccelerator works with Ray Tune."""
    accelerator = RayAccelerator(num_workers=2, use_gpu=False)
    callbacks = [TuneReportCallback(on="validation_end")]
    analysis = tune.run(
        train_func(tmpdir, accelerator, callbacks=callbacks),
        config={
            "max_epochs": tune.choice([1, 2, 3])
        },
        resources_per_trial={
            "cpu": 0,
            "extra_cpu": 2
        },
        num_samples=2
    )
    assert all(analysis.results_df["training_iteration"] ==
               analysis.results_df["config.max_epochs"])