from unittest import TestCase

from kubernetes.client import V1Deployment, V1ObjectMeta

from django_operator.tests.base import PropObject
from django_operator.utils import (
    _k8s_client_owner_mask,
    merge,
    slugify,
    superget,
)


class UtilsTestCase(TestCase):
    def test_superget(self):
        haystack = {
            "a": {
                "b": {
                    "c": {
                        "d": "needle",
                    },
                    "e": "otherneedle",
                }
            },
            "f": "thirdneedle",
        }
        self.assertEqual(superget(haystack, "f"), "thirdneedle")
        self.assertEqual(superget(haystack, "a.b.e"), "otherneedle")
        self.assertEqual(superget(haystack, "a.b.c.d"), "needle")
        self.assertEqual(superget(haystack, "a.b.g", default={}), {})
        self.assertEqual(superget(haystack, "n", default=""), "")

        prop_haystack = PropObject(haystack)
        self.assertEqual(superget(prop_haystack, "f"), "thirdneedle")
        self.assertEqual(superget(prop_haystack, "a.b.e"), "otherneedle")
        self.assertEqual(superget(prop_haystack, "a.b.c.d"), "needle")
        self.assertEqual(superget(prop_haystack, "a.b.g", default={}), {})
        self.assertEqual(superget(prop_haystack, "n", default=""), "")

    def test_real_superget(self):
        haystack = {
            "complete_management_commands": {},
            "condition": "migrating",
            "created": {
                "deployment": {"app": "app-ee4b5ef0", "redis": "redis"},
                "horizontalpodautoscaler": {"app": "app"},
                "service": {"redis": "redis"},
            },
            "kopf": {
                "dummy": "2021-12-30T19:59:16.218895",
                "progress": {
                    "begin_migration": {
                        "failure": False,
                        "purpose": "update",
                        "retries": 1,
                        "started": "2021-12-30T19:52:14.964015",
                        "stopped": "2021-12-30T19:52:14.967186",
                        "success": True,
                    },
                    "green_app_ready": {
                        "delayed": "2021-12-30T19:59:16.214597",
                        "failure": False,
                        "message": "Missing the required parameter `name`",
                        "purpose": "update",
                        "retries": 7,
                        "started": "2021-12-30T19:52:14.964035",
                        "success": False,
                    },
                },
            },
            "migrateToSpec": {
                "alwaysRunMigrations": True,
                "appProbeSpec": {
                    "failureThreshold": 3,
                    "httpGet": {
                        "httpHeaders": [
                            {"name": "Host", "value": "testbed.money-positive.net"}
                        ],
                        "path": "/privacy/",
                        "port": 8000,
                        "scheme": "HTTP",
                    },
                    "initialDelaySeconds": 10,
                    "periodSeconds": 20,
                    "timeoutSeconds": 2,
                },
                "autoscalers": {
                    "app": {
                        "cpuUtilizationThreshold": 60,
                        "enabled": True,
                        "replicas": {"maximum": 2, "minimum": 1},
                    },
                    "worker": {
                        "cpuUtilizationThreshold": 60,
                        "enabled": False,
                        "replicas": {"maximum": 10, "minimum": 1},
                    },
                },
                "clusterIssuer": "letsencrypt",
                "commands": {
                    "app": {
                        "args": [
                            "money_positive.wsgi:application",
                            "--bind",
                            "0.0.0.0:8000",
                        ],
                        "command": ["gunicorn"],
                    },
                    "beat": {
                        "args": [
                            "--app=money_positive",
                            "beat",
                            "--loglevel=INFO",
                            "--scheduler",
                            "django_celery_beat.schedulers:DatabaseScheduler",
                            "--pidfile=/tmp/celerybeat.pid",
                        ],
                        "command": ["celery"],
                    },
                    "worker": {
                        "args": ["--app=money_positive", "worker", "--loglevel=INFO"],
                        "command": ["celery"],
                    },
                },
                "env": [],
                "envFromConfigMapRefs": ["env"],
                "envFromSecretRefs": [
                    "aws",
                    "crypto",
                    "database",
                    "email",
                    "google-auth",
                    "paysimple",
                    "plaid",
                    "secret-key",
                ],
                "host": "testbed.money-positive.net",
                "image": "registry.gitlab.com/money-positive/mp-app",
                "imagePullSecrets": [{"name": "gitlab-registry-read"}],
                "initManageCommands": [
                    ["migrate"],
                    ["create_groups"],
                    ["seed_live_test"],
                    ["loaddata", "money_positive/fixtures/us_states.json"],
                ],
                "initManageTimeouts": {"iterations": 20, "period": 12},
                "ports": {"app": 8000, "redis": 6379},
                "resourceRequests": {
                    "app": {"cpu": "100m", "memory": "200Mi"},
                    "beat": {"cpu": "10m", "memory": "200Mi"},
                    "worker": {"cpu": "30m", "memory": "250Mi"},
                },
                "version": "ee4b5ef0",
                "volumeMounts": [
                    {
                        "mountPath": "/app/src/secret/",
                        "name": "google-drive-client-secret",
                        "readOnly": True,
                    }
                ],
                "volumes": [
                    {
                        "name": "google-drive-client-secret",
                        "secret": {"secretName": "google-drive"},
                    }
                ],
            },
            "migrationVersion": "ee4b5ef0",
        }
        self.assertEqual(superget(haystack, "created.deployment.app"), "app-ee4b5ef0")

    def test_merge(self):
        target = {"a": 1, "b": {"c": {"d": [{}, {}]}}}
        extension = {
            "a": 2,
            "b": {"c": {"e": 3, ("d", 1): {"stuff": "second"}}, "l": "p"},
            "h": "q",
        }
        merge(target, extension)
        self.assertEqual(
            target,
            {
                "a": 2,
                "b": {
                    "c": {"d": [{}, {"stuff": "second"}], "e": 3},
                    "l": "p",
                },
                "h": "q",
            },
        )

    def test_complex_merge(self):
        target = {
            "spec": {
                "initContainers": [],
            }
        }
        extension = {"spec": {"imagePullSecrets": [{"name": "test-value"}]}}
        merge(target, extension)
        self.assertEqual(
            target,
            {
                "spec": {
                    "initContainers": [],
                    "imagePullSecrets": [{"name": "test-value"}],
                }
            },
        )

    def test_slugify(self):
        unslug = "bu.nch_of1  OTHEr__shit"
        self.assertEqual(slugify(unslug), "bu-nch-of1-other-shit")

    def test_k8s_client_owner_mask(self):
        k8s_obj = V1Deployment(
            api_version="app/v1",
            kind="Deployment",
            metadata=V1ObjectMeta(
                name="poopydeployment",
                uid="asdf-sdaf-sadf-sadf-dsaf",
            ),
        )

        self.assertEqual(
            _k8s_client_owner_mask(k8s_obj),
            {
                "apiVersion": "app/v1",
                "kind": "Deployment",
                "metadata": {
                    "name": "poopydeployment",
                    "uid": "asdf-sdaf-sadf-sadf-dsaf",
                },
            },
        )
