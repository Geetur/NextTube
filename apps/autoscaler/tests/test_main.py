"""Unit tests for local worker autoscaling decisions."""

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch


def load_module():
    """Load the controller with external service clients replaced by stubs."""
    fake_docker = ModuleType("docker")
    fake_docker.DockerClient = object
    fake_docker.from_env = lambda: None

    fake_kafka = ModuleType("confluent_kafka")
    fake_kafka.Consumer = object
    fake_kafka.TopicPartition = object

    fake_config = ModuleType("config")
    fake_config.settings = SimpleNamespace(
        autoscaler_min_workers=1,
        autoscaler_max_workers=3,
        autoscaler_target_lag_per_worker=2,
        autoscaler_scale_up_cooldown_seconds=15,
        autoscaler_scale_down_cooldown_seconds=300,
    )

    module_path = Path(__file__).resolve().parents[1] / "main.py"
    spec = importlib.util.spec_from_file_location("autoscaler_main_under_test", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load autoscaler module")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(
        "sys.modules",
        {"docker": fake_docker, "confluent_kafka": fake_kafka, "config": fake_config},
    ):
        spec.loader.exec_module(module)
    return module


class AutoscalerDecisionTests(unittest.TestCase):
    """Verify replica bounds and cooldown protection without Docker or Kafka."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module()

    def test_desired_replicas_respects_minimum_and_partition_cap(self) -> None:
        self.assertEqual(self.module.desired_replicas(0), 1)
        self.assertEqual(self.module.desired_replicas(3), 2)
        self.assertEqual(self.module.desired_replicas(99), 3)

    def test_scale_up_waits_for_its_cooldown(self) -> None:
        self.assertFalse(self.module.should_scale_up(1, 3, now=14, last_scale_at=0))
        self.assertTrue(self.module.should_scale_up(1, 3, now=15, last_scale_at=0))

    def test_scale_down_requires_zero_lag_for_full_cooldown(self) -> None:
        self.assertFalse(
            self.module.should_scale_down(0, 3, 1, now=299, last_nonzero_lag_at=0)
        )
        self.assertFalse(
            self.module.should_scale_down(1, 3, 1, now=301, last_nonzero_lag_at=0)
        )
        self.assertTrue(
            self.module.should_scale_down(0, 3, 1, now=300, last_nonzero_lag_at=0)
        )