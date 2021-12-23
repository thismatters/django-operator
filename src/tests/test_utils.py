from unittest import TestCase

from ..utils import merge, slugify, superget


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
        self.assertEqual(superget(haystack, "n", default=""), "")

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
