from unittest import TestCase
from unittest.mock import patch

import kopf

from django_operator.pipelines.base import BasePipeline, BaseWaitingStep
from django_operator.tests.base import MockLogger, MockPatch


class ThingWithName:
    name = "i-have-a-name"

    def __init__(self, **kwargs):
        pass

    def handle(self, **kwargs):
        pass


class OtherThingWithName(ThingWithName):
    name = "i-also-have-a-name"


class BasePipelineTestCase(TestCase):
    def setUp(self):
        super().setUp()
        self.kwargs = {
            "logger": MockLogger(),
            "patch": MockPatch(),
            "status": {},
            "labels": {},
            "diff": (),
            "body": {},
            "spec": {"original": "spec"},
        }

    def test_spec_handling_new(self):
        """Ensure that the spec is perserved during a pipeline"""
        pipeline = BasePipeline(**self.kwargs)
        self.assertEqual(pipeline.spec, self.kwargs["spec"])
        self.assertEqual(
            self.kwargs["patch"].status, {"pipelineSpec": self.kwargs["spec"]}
        )
        self.assertTrue(hasattr(pipeline, "logger"))
        self.assertTrue(hasattr(pipeline, "patch"))
        self.assertTrue(hasattr(pipeline, "labels"))
        self.assertTrue(hasattr(pipeline, "diff"))
        self.assertTrue(hasattr(pipeline, "body"))
        self.kwargs["body"] = {"poop": True}

    def test_spec_handling_repeat(self):
        """Ensure that the spec is perserved during a pipeline"""
        existing = {"existing": "spec"}
        self.kwargs["status"] = {"pipelineSpec": existing}
        pipeline = BasePipeline(**self.kwargs)
        self.assertEqual(pipeline.spec, existing)
        self.assertEqual(self.kwargs["patch"].status, {})

    @patch.object(BasePipeline, "label", "test-pipeline")
    @patch.object(BasePipeline, "steps", [ThingWithName, OtherThingWithName])
    def test_step_names_basic(self):
        pipeline = BasePipeline(**self.kwargs)
        self.assertEqual(pipeline.step_names, ["i-have-a-name", "i-also-have-a-name"])

        self.assertTrue(BasePipeline.is_step_name(labels={"test-pipeline": "ready"}))
        self.assertTrue(BasePipeline.is_step_name(labels={"test-pipeline": "done"}))
        self.assertTrue(BasePipeline.is_step_name(labels={"test-pipeline": "i-have-a-name"}))
        self.assertFalse(
            BasePipeline.is_step_name(labels=
                {"test-pipeline": "fuck-you-i-wont-do-what-you-tell-me"}
            )
        )

    @patch.object(BasePipeline, "steps", [ThingWithName, OtherThingWithName])
    def test_resolve_step_name(self):
        pipeline = BasePipeline(**self.kwargs)
        deets = pipeline.resolve_step("i-have-a-name")
        self.assertEqual(deets.klass, ThingWithName)
        self.assertEqual(deets.next_step_name, "i-also-have-a-name")
        self.assertEqual(deets.index, 0)

        deets = pipeline.resolve_step("i-also-have-a-name")
        self.assertEqual(deets.klass, OtherThingWithName)
        self.assertEqual(deets.next_step_name, "done")
        self.assertEqual(deets.index, 1)

    @patch.object(BasePipeline, "has_real_changes", lambda *_: False)
    @patch.object(BasePipeline, "initiate_pipeline")
    def test_handle_initiate_sieve(self, p_initiate_pipeline):
        pipeline = BasePipeline(**self.kwargs)
        # should not run for trivial changes
        self.assertIsNone(pipeline.handle_initiate())
        p_initiate_pipeline.assert_not_called()

    @patch.object(BasePipeline, "has_real_changes", lambda *_: True)
    @patch.object(BasePipeline, "initiate_pipeline")
    def test_handle_initiate_allow(self, p_initiate_pipeline):
        pipeline = BasePipeline(**self.kwargs)
        # should not run for trivial changes
        pipeline.handle_initiate()
        p_initiate_pipeline.assert_called()

    @patch.object(BasePipeline, "label", "test-pipeline")
    @patch.object(BasePipeline, "steps", [ThingWithName, OtherThingWithName])
    def test_initiate_pipeline(self):
        pipeline = BasePipeline(**self.kwargs)
        pipeline.initiate_pipeline()
        self.assertEqual(
            self.kwargs["patch"].metadata.labels, {"test-pipeline": "i-have-a-name"}
        )

    def test_has_real_changes_false(self):
        self.kwargs["diff"] = (
            ("update", ("metadata", "labels", "test-pipeline"), "something", "ready"),
            ("add", ("metadata",), None, {"labels": {"test-pipeline": "ready"}}),
        )
        pipeline = BasePipeline(**self.kwargs)
        self.assertFalse(pipeline.has_real_changes())

    def test_has_real_changes_true(self):
        self.kwargs["diff"] = (("update", ("spec", "version"), "old", "new"),)
        pipeline = BasePipeline(**self.kwargs)
        self.assertTrue(pipeline.has_real_changes())

    @patch.object(BasePipeline, "finalize_pipeline")
    def test_handle_finalize(self, p_finalize_pipeline):
        ctx = {"stuff": "happened"}
        self.kwargs["status"] = {"pipeline": ctx}
        pipeline = BasePipeline(**self.kwargs)
        pipeline.handle_finalize()
        p_finalize_pipeline.assert_called_once_with(context=ctx)

    @patch.object(BasePipeline, "label", "test-pipeline")
    @patch.object(BasePipeline, "_handle")
    @patch.object(BasePipeline, "handle_finalize")
    @patch.object(BasePipeline, "handle_initiate")
    def test_handle_start(self, p_handle_initiate, p_handle_finalize, p_handle):
        self.kwargs["labels"] = {"test-pipeline": "ready"}
        pipeline = BasePipeline(**self.kwargs)
        pipeline.handle()
        p_handle_initiate.assert_called_once_with()
        p_handle_finalize.assert_not_called()
        p_handle.assert_not_called()

    @patch.object(BasePipeline, "label", "test-pipeline")
    @patch.object(BasePipeline, "_handle")
    @patch.object(BasePipeline, "handle_finalize")
    @patch.object(BasePipeline, "handle_initiate")
    def test_handle_end(self, p_handle_initiate, p_handle_finalize, p_handle):
        self.kwargs["labels"] = {"test-pipeline": "done"}
        pipeline = BasePipeline(**self.kwargs)
        pipeline.handle()
        p_handle_initiate.assert_not_called()
        p_handle_finalize.assert_called_once_with()
        p_handle.assert_not_called()

    @patch.object(BasePipeline, "label", "test-pipeline")
    @patch.object(BasePipeline, "_handle")
    @patch.object(BasePipeline, "handle_finalize")
    @patch.object(BasePipeline, "handle_initiate")
    def test_handle(self, p_handle_initiate, p_handle_finalize, p_handle):
        self.kwargs["labels"] = {"test-pipeline": "asdf"}
        pipeline = BasePipeline(**self.kwargs)
        pipeline.handle()
        p_handle_initiate.assert_not_called()
        p_handle_finalize.assert_not_called()
        p_handle.assert_called_once_with("asdf")

    @patch.object(BasePipeline, "label", "test-pipeline")
    @patch.object(BasePipeline, "steps", [ThingWithName, OtherThingWithName])
    @patch.object(ThingWithName, "handle")
    def test__handle(self, p_step_handle):
        p_step_handle.return_value = {"good": "stuff"}
        ctx = {"stuff": "happened"}
        self.kwargs["status"] = {"pipeline": ctx}
        pipeline = BasePipeline(**self.kwargs)
        ret = pipeline._handle("i-have-a-name")
        p_step_handle.assert_called_once_with(context=ctx)
        self.assertEqual(ret, {"good": "stuff"})
        self.assertEqual(
            self.kwargs["patch"].metadata.labels,
            {"test-pipeline": "i-also-have-a-name"},
        )


class BaseWaitingStepTestCase(TestCase):
    def setUp(self):
        super().setUp()
        self.kwargs = {
            "logger": MockLogger(),
            "patch": MockPatch(),
            "status": {},
            "labels": {},
            "diff": (),
            "body": {},
            "spec": {"original": "spec"},
        }

    @patch.object(BaseWaitingStep, "is_ready")
    @patch.object(BaseWaitingStep, "_check_timeout")
    def test_handle_not_ready(self, p_check_timeout, p_is_ready):
        p_is_ready.return_value = False
        step = BaseWaitingStep(**self.kwargs)
        with self.assertRaises(kopf.TemporaryError):
            step.handle(context={"stuff": "happened"})
        p_is_ready.assert_called_once_with(context={"stuff": "happened"})
        p_check_timeout.assert_called_once_with()

    @patch.object(BaseWaitingStep, "is_ready")
    @patch.object(BaseWaitingStep, "_check_timeout")
    def test_handle_ready(self, p_check_timeout, p_is_ready):
        p_is_ready.return_value = True
        step = BaseWaitingStep(**self.kwargs)
        self.assertEqual(step.handle(context={"stuff": "happened"}), {})
        p_is_ready.assert_called_once_with(context={"stuff": "happened"})

    @patch.object(BaseWaitingStep, "iterations_default", 10)
    def test_check_timeout_fine(self):
        self.kwargs["retry"] = 12
        self.kwargs["spec"] = {"pipelineStep": {"iterations": 13}}
        step = BaseWaitingStep(**self.kwargs)
        step._check_timeout()

    @patch.object(BaseWaitingStep, "iterations_default", 10)
    def test_check_timeout_overlong_default(self):
        self.kwargs["retry"] = 12
        # self.kwargs["spec"] = {"pipelineStep": {"iterations": 13}}
        step = BaseWaitingStep(**self.kwargs)
        with self.assertRaises(kopf.PermanentError):
            step._check_timeout()

    @patch.object(BaseWaitingStep, "iterations_default", 15)
    def test_check_timeout_overlong_specified(self):
        self.kwargs["retry"] = 12
        self.kwargs["spec"] = {"pipelineStep": {"iterations": 10}}
        step = BaseWaitingStep(**self.kwargs)
        with self.assertRaises(kopf.PermanentError):
            step._check_timeout()
