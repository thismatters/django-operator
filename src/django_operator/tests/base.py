class MockLogger:
    def info(self, *args, **kwargs):
        pass

    def debug(self, *args, **kwargs):
        pass


class PropObject:
    def __init__(self, dct):
        for prop, value in dct.items():
            if isinstance(value, (dict,)) and value:
                _value = PropObject(value)
            else:
                _value = value
            setattr(self, prop, _value)

    def to_dict(self):
        pass


class MockPatch(PropObject):
    def __init__(self):
        template = {"status": {}, "metadata": {"labels": {}}}
        super().__init__(template)
