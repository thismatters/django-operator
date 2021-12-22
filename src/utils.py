class WaitedTooLongException(Exception):
    pass


def superget(dct, superkey, *, default=None, _raise=None):
    if "." in superkey:
        key, remainder = superkey.split(".", maxsplit=1)
    else:
        key = superkey
        remainder = None
    if key not in dct:
        if _raise is not None:
            raise _raise
        return default
    if not remainder:
        return dct[key]
    return superget(dct[key], remainder)


def merge(left, right):
    """recursively put all keys (subkeys, etc.) from right into left. If
    keys on the right are tuples then the data structure on the left will be
    traversed"""
    for key_, value_ in right.items():
        if isinstance(key_, (tuple,)):
            print("tuple")
            # traverse the data structure on the left
            _key, *indices = key_
            _value = left[_key]
            for index in indices:
                print(index)
                _value = _value[index]
            _value = merge(_value, value_)
            continue
        if key_ not in left:
            left[key_] = value_
        _value = left[key_]
        if type(_value) != type(value_):
            raise ValueError
        if isinstance(_value, (dict,)):
            _value = merge(_value, value_)
        elif isinstance(_value, (list,)):
            _value.extend(value_)
        else:
            # primitive type, probably not a pointer
            left[key_] = value_


class MergeDict(dict):
    # this can't handle editing dicts within lists, so I'm dumping it.
    def __init__(self, **data):
        super().__init__(**data)
        for k, v in data.items():
            if isinstance(v, (dict,)):
                self[k] = self.__class__(**v)
            else:
                self[k] = v

    def merge(self, other):
        for key, value in other.items():
            if key in self:
                _value = self[key]
                if isinstance(_value, (dict,)) and isinstance(value, (dict,)):
                    self[key].merge(value)
                elif type(_value) != type(value):
                    raise ValueError
                elif isinstance(value, (list,)):
                    self[key].extend(value)
                else:
                    self[key] = value
            else:
                self.update({key: value})


if __name__ == "__main__":
    orig = {"a": 1, "b": {"c": {"d": [{}, {}]}}}
    # _orig = MergeDict(**orig)
    merge(
        orig,
        {
            "a": 2,
            "b": {"c": {"e": 3, ("d", 1): {"stuff": "second"}}, "l": "p"},
            "h": "q",
        },
    )
    print(orig)

    print(superget(orig, "b.c.d"))
