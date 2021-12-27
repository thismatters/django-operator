import re


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
            continue
        _value = left[key_]
        if type(_value) != type(value_):
            raise ValueError(f"type mismatch for key {key_}")
        if isinstance(_value, (dict,)):
            _value = merge(_value, value_)
        elif isinstance(_value, (list,)):
            _value.extend(value_)
        else:
            # primitive type, probably not a pointer
            left[key_] = value_


def slugify(unslug):
    return re.sub("[^-a-z0-9]+", "-", unslug.lower())
