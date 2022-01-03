import kopf

from django_operator.utils import superget


class BasePipelineStep:
    name = None
    attribute_kwargs = ("logger", "patch", "status", "retry", "spec")

    def __init__(self, **kwargs):
        for attr in self.attribute_kwargs:
            setattr(self, attr, kwargs.get(attr))
        self.kwargs = kwargs
        super().__init__()

    def handle(self):
        raise NotImplementedError()


class BaseWaitingStep(BasePipelineStep):
    iterations_key = "pipelineStep.iterations"
    iterations_default = 20
    period_key = "pipelineStep.period"
    period_default = 6
    pipeline_step_noun = "pipeline step"

    def _check_timeout(self):
        max_retries = superget(
            self.spec, self.iterations_key, default=self.iterations_default
        )
        self.logger.info(f"Retry count {self.retry}")
        if self.retry >= max_retries:
            self.patch.status["condition"] = "degraded"
            raise kopf.PermanentError(
                f"{self.pipeline_step_noun} took too long. "
                "Manual intervention required!"
            )

    def is_ready(self, *, context):
        raise NotImplementedError()

    def handle(self, *, context):
        if not self.is_ready(context=context):
            self._check_timeout()
            period = superget(self.spec, self.period_key, default=self.period_default)
            raise kopf.TemporaryError(
                f"The {self.pipeline_step_noun} is not complete. Waiting.", delay=period
            )
        return {}


class StepDetails:
    def __init__(self, *, index, name, klass, next_step_name):
        self.index = index
        self.name = name
        self.klass = klass
        self.next_step_name = next_step_name


class BasePipeline:
    label = None
    waiting_step_name = "ready"
    complete_step_name = "done"
    steps = []
    attribute_kwargs = ("logger", "patch", "status", "labels", "diff", "body")
    update_handler_name = "pipeline"

    def __init__(self, **kwargs):
        self._spec = kwargs.pop("spec")
        for attr in self.attribute_kwargs:
            setattr(self, attr, kwargs.get(attr))
        spec = self.status.get("pipelineSpec")
        if spec is None:
            spec = self._spec
        self.spec = spec
        kwargs.update({"spec": spec})
        self.kwargs = kwargs
        super().__init__()

    @classmethod
    def is_step_name(cls, value, **_):
        if value in (cls.waiting_step_name, cls.complete_step_name):
            return True
        for step in cls.steps:
            if value == step.name:
                return True
        return False

    def initiate_pipeline(self):
        kopf.info(self.body, reason="Migrating", message="Enacting new config")
        self.logger.info(
            f"Migrating from {self.status.get('version', 'new')} to "
            f"{self.spec.get('version')}"
        )
        self.patch.status["pipelineSpec"] = dict(self.spec)
        self.patch.metadata.labels[self.label] = self.steps[0].name

    def finalize_pipeline(self, *, context):
        self.patch.status[self.update_handler_name] = None
        return None

    def resolve_step(self, step_name):
        step_names = [s.name for s in self.steps]
        step_index = step_names.index(step_name)
        step_klass = self.steps[step_index]
        if step_index + 1 == len(self.steps):
            next_step = self.complete_step_name
        else:
            next_step = step_names[step_index + 1]
        return StepDetails(
            index=step_index, name=step_name, klass=step_klass, next_step_name=next_step
        )

    def has_real_changes(self):
        for action, field, old, new in self.diff:
            if field[0] != "metadata":
                self.logger.info(
                    f"Non metadata field {action} :: {field} := {old} -> {new}"
                )
                return True
        return False

    def handle_initiate(self):
        if self.has_real_changes():
            return self.initiate_pipeline()
        else:
            self.logger.info(
                f"Changes appear to only touch {self.label} labels; skipping"
            )
            return None

    def handle_finalize(self):
        context = self.status.get(self.update_handler_name, {})
        return self.finalize_pipeline(context=context)

    def _handle(self, step_name):
        # pull context from all prior handler run
        context = self.status.get(self.update_handler_name, {})
        step_details = self.resolve_step(step_name)
        # run the step handler
        ret = step_details.klass(**self.kwargs).handle(context=context)
        # set the label to trigger next step
        self.patch.metadata.labels[self.label] = step_details.next_step_name
        return ret

    def handle(self):
        # get label value
        step_name = self.labels.get(self.label)
        self.logger.info(f"Running pipeline step {step_name!r}")
        if step_name == self.waiting_step_name:
            return self.handle_initiate()
        if step_name == self.complete_step_name:
            return self.handle_finalize()
        return self._handle(step_name)
