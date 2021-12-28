from unittest import TestCase

from django_operator.kinds import DjangoKind


class MockLogger:
    def info(self, *args, **kwargs):
        pass

    def debug(self, *args, **kwargs):
        pass


class DjangoKindTestCase(TestCase):
    def test_deployment_names_new_version(self):
        status = {
            "created": {"deployment": {"app": "app-6-9-420", "beat": "beat-6.8.4"}}
        }
        django_kind = DjangoKind(
            logger=MockLogger(),
            status=status,
            patch={},
            body={},
            spec={
                "host": "test.somewhere.com",
                "clusterIssuer": "letsencrypt",
                "version": "6.9.421",
                "image": "testimage",
            },
            namespace="test",
        )
        former, existing = django_kind._deployment_names(
            purpose="app",
        )
        self.assertEqual(former, "app-6-9-420")
        self.assertEqual(existing, None)

    def test_deployment_names_current_version(self):
        status = {
            "created": {"deployment": {"app": "app-6-9-420", "beat": "beat-6-8-4"}}
        }
        django_kind = DjangoKind(
            logger=MockLogger(),
            status=status,
            patch={},
            body={},
            spec={
                "host": "test.somewhere.com",
                "clusterIssuer": "letsencrypt",
                "version": "6.9.420",
                "image": "testimage",
            },
            namespace="test",
        )
        former, existing = django_kind._deployment_names(
            purpose="app",
        )
        self.assertEqual(former, None)
        self.assertEqual(existing, "app-6-9-420")
