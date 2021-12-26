from unittest import TestCase

from django_operator.kinds import DjangoKind


class DjangoKindTestCase(TestCase):
    def test_deployment_names_new_version(self):
        status = {
            "created": {"deployment": {"app": "app-6-9-420", "beat": "beat-6.8.4"}}
        }
        base_kwargs = {"version_slug": "6-9-421"}
        django_kind = DjangoKind(logger=None)
        former, existing = django_kind._deployment_names(
            purpose="app",
            status=status,
            base_kwargs=base_kwargs,
        )
        self.assertEqual(former, "app-6-9-420")
        self.assertEqual(existing, None)

    def test_deployment_names_current_version(self):
        status = {
            "created": {"deployment": {"app": "app-6-9-420", "beat": "beat-6-8-4"}}
        }
        base_kwargs = {"version_slug": "6-9-420"}
        django_kind = DjangoKind(logger=None)
        former, existing = django_kind._deployment_names(
            purpose="app",
            status=status,
            base_kwargs=base_kwargs,
        )
        self.assertEqual(former, None)
        self.assertEqual(existing, "app-6-9-420")
