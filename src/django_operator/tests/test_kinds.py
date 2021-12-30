from unittest import TestCase
from unittest.mock import patch

from django_operator.kinds import DjangoKind
from django_operator.services import PodService

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
        former, existing = django_kind._resource_names(
            kind="deployment",
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
        former, existing = django_kind._resource_names(
            kind="deployment",
            purpose="app",
        )
        self.assertEqual(former, None)
        self.assertEqual(existing, "app-6-9-420")

    @patch.object(PodService, "ensure")
    def test_ensure_kwargs(self, p_ensure):
        status = {
            "created": {"deployment": {"app": "app-6-9-420", "beat": "beat-6-8-4"}}
        }
        django_kind = DjangoKind(
            logger=MockLogger(),
            status=status,
            patch={},
            body={"this": "body"},
            spec={
                "host": "test.somewhere.com",
                "clusterIssuer": "letsencrypt",
                "version": "6.9.420",
                "image": "testimage",
            },
            namespace="test",
        )

        django_kind.clean_manage_commands(pod_name="poopypod")
        p_ensure.assert_called_once_with(
            namespace="test",
            template="pod_migrations.yaml",
            parent={"this": "body"},
            purpose="migrations",
            delete=True,
            existing="poopypod",
            **django_kind.base_kwargs,
        )
