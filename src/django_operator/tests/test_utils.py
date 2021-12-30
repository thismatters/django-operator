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
