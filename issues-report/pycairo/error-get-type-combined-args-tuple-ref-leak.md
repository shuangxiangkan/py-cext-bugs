# Reference leak in `error_get_type_combined()` argument tuple

I found a reference leak in `error_get_type_combined()`. The argument tuple built
for `PyType_Type.tp_new()` is never released.

File: `cairo/error.c`

Function: `error_get_type_combined`

Relevant code:

```c
class_dict = PyDict_New ();
if (class_dict == NULL)
    return NULL;

new_type_args = Py_BuildValue ("s(OO)O", name,
                               error, other, class_dict);
Py_DECREF (class_dict);
if (new_type_args == NULL)
    return NULL;

new_type = PyType_Type.tp_new (&PyType_Type, new_type_args, NULL);
return new_type;
```

`Py_BuildValue()` returns a new owned tuple. `PyType_Type.tp_new()` receives it
as its `args` argument and does not steal the caller's reference. The function
returns without decref'ing `new_type_args`, leaking one tuple per call.

This is a smaller leak than the others — `error_get_type_combined()` runs during
module initialization to build the exception-type hierarchy, so it leaks a small
fixed amount once rather than on a hot path. It is still a straightforward
owned-reference cleanup bug.

Suggested fix — decref the args tuple after the call:

```c
new_type = PyType_Type.tp_new (&PyType_Type, new_type_args, NULL);
Py_DECREF (new_type_args);
return new_type;
```
