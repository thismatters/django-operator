import re

from kopf import (
    adjust_namespace,
    append_owner_reference,
    harmonize_naming,
    label,
)


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
    val = dct[key]
    if not remainder:
        return val
    return superget(val, remainder, default=default, _raise=_raise)


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


def _k8s_client_owner_mask(k8s_obj):
    """The kubernetes python client puts keys in snake_case, while kopf
    wants them camelCased... so shim it up. The only incompatibility is the
    `append_owner_references` function, so only the owner related stuff is
    really needed"""
    _owner = {}
    keys = ("api_version", "kind", "metadata.name", "metadata.uid")
    name_map = {
        "api_version": "apiVersion",
    }
    for key in keys:
        val = superget(k8s_obj, key)
        _pointer = _owner
        *addresses, key_proper = key.split(".")
        for address in addresses:
            _pointer = _pointer.setdefault(address, {})
        _pointer[name_map.get(key_proper, key_proper)] = val
    return _owner


def adopt_sans_labels(objs, owner, *, labels=None):
    owner_mask = owner
    if not isinstance(owner, (dict,)):
        if hasattr(owner, "to_dict"):
            owner = owner.to_json()
            owner_mask = _k8s_client_owner_mask(owner)
    owner_name = superget(owner, "metadata.name")
    owner_namespace = superget(owner, "metadata.namespace")
    owner_labels = dict(superget(owner, "metadata.labels", default={}))
    append_owner_reference(objs, owner=owner_mask)
    harmonize_naming(objs, name=owner_name)
    adjust_namespace(objs, namespace=owner_namespace)

    if labels:
        for _label in labels:
            owner_labels.pop(_label, None)
    label(objs, labels=owner_labels)
