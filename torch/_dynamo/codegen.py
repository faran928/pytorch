# mypy: allow-untyped-defs
import collections
import dataclasses
import re
import sys
import types
from typing import Counter, Dict, List, Optional

import torch.nn

from . import utils
from .bytecode_transformation import (
    add_push_null,
    add_push_null_call_function_ex,
    create_call_function,
    create_call_method,
    create_dup_top,
    create_instruction,
    create_load_const,
    create_load_method,
    create_rot_n,
    Instruction,
)
from .exc import unimplemented
from .source import AttrSource, Source
from .utils import is_safe_constant, rot_n_helper
from .variables.base import ValueMutationExisting, VariableTracker
from .variables.functions import FunctionDecoratedByContextlibContextManagerVariable
from .variables.nn_module import NNModuleVariable
from .variables.tensor import (
    NumpyNdarrayVariable,
    SymNodeVariable,
    TensorVariable,
    UnspecializedPythonVariable,
)
from .variables.torch_function import TensorWithTFOverrideVariable


@dataclasses.dataclass
class GraphOutputEntry:
    index: int
    variable: VariableTracker


class PyCodegen:
    """
    Helper class uses for constructing Python bytecode
    """

    def __init__(
        self,
        tx=None,
        root: Optional[torch.nn.Module] = None,
        graph_output_var: Optional[str] = None,
        tempvars=None,
        overridden_sources=None,
    ) -> None:
        self.root = root
        self.top_of_stack: Optional[VariableTracker] = None
        self.uses: Counter[VariableTracker] = collections.Counter()
        self.graph_outputs: Dict[int, GraphOutputEntry] = {}
        self._output: List[Instruction] = []
        # This determines which VariableTracker should be stored as locals, and
        # maps the VariableTracker to the local variable name. Note that it
        # could map to None initially, in which case we'll overwrite it to map
        # to real temporary names via `add_cache`.
        self.tempvars = tempvars or {}
        self.tx = tx
        self.graph_output_var = graph_output_var
        self.code_options = self.tx.output.code_options
        self.cell_and_freevars = self.tx.cell_and_freevars
        self.new_var = self.tx.output.new_var
        self.value_from_source: bool = True
        # This serves as a way for codegen to use a different source; we need
        # this because sometimes we can't easily modify the original source
        # without affecting other components, e.g., guards.
        self.overridden_sources: Dict[Source, Source] = overridden_sources or {}

    def restore_stack(self, stack_values, *, value_from_source=True):
        prev = self.value_from_source
        self.value_from_source &= value_from_source
        try:
            self.foreach(stack_values)
        finally:
            self.value_from_source = prev

    def graph_output_vars(self):
        return [x.variable for x in self.graph_outputs.values()]

    def call_reconstruct(self, value):
        res = value.reconstruct(self)
        assert res is None, f"reconstruct!=None {value}"

    def add_push_null(self, gen_fn, call_function_ex=False):
        """
        `gen_fn` generates instructions via PyCodegen methods
        that push a single callable to the stack.

        `add_push_null` pushes a NULL to the stack before or after the
        instructions generated by `gen_fn`, depending on Python version.

        Will attempt to use the NULL push bit for instructions
        with such bits (LOAD_GLOBAL 3.11+, LOAD_ATTR 3.12+, LOAD_SUPER_ATTR).
        """
        old_len = len(self._output)
        if sys.version_info < (3, 13):
            # gen_fn may DUP_TOP instead if TOS is not cleared.
            # Will cause problems since NULL will be pushed right
            # before the generated instructions in <= 3.12
            self.clear_tos()
        gen_fn()
        # inplace modify self._output
        added_insts = self._output[old_len:]
        del self._output[old_len:]
        if call_function_ex:
            self._output.extend(add_push_null_call_function_ex(added_insts))
        else:
            self._output.extend(add_push_null(added_insts))
        if sys.version_info >= (3, 13):
            # NULL will be at top of stack
            self.clear_tos()

    def __call__(self, value, allow_cache=True):
        """
        Generate code such that top-of-stack (TOS) is set to value.

        `allow_cache` is used to determine whether the following could happen,
        when `value` is a `VariableTracker`:
        1. if `value` was codegen-ed previously with `allow_cache=True` and
           without using source, reuse the generated code by loading from top
           of stack or tempvars.
        2. emit code based on `value.source` to handle aliasing.

        Notable effects:
        1. `self.top_of_stack` will be set to `value`, if we don't codegen
           `value` based on source.
        2. `self.uses[value]` will increment, if we don't codegen `value` based
           on source or cache/top-of-stack reuse; in other words, if we codegen
           as if `value` is modelling some brand new python value.
        """
        if isinstance(value, Source):
            # If the source needs to be overridden, use the new one.
            source = self.overridden_sources.get(value, value)
            self.call_reconstruct(source)
            # We don't support dup_top optimization for source yet.
            self.clear_tos()
            return

        assert isinstance(value, VariableTracker)
        output = self._output
        graph_outputs = self.graph_outputs

        if allow_cache:
            if self.top_of_stack is value:
                output.append(create_dup_top())
                return

            if self.tempvars.get(value) is not None:
                output.append(self.create_load(self.tempvars[value]))
                self.top_of_stack = value
                return

        # Dynamo normally prefers codegen from source to account for aliasing.
        if (
            value.source is not None
            and allow_cache
            and not isinstance(
                value, FunctionDecoratedByContextlibContextManagerVariable
            )
        ):
            # There's a corner case for export: for instance, if the computation
            # graph is just identity on an input tensor, Dynamo would just emit
            # a `LOAD_FAST` from the input source, rather than generating an
            # identity FX graph.
            #
            # However, export wants to maximize graph capture; in the case
            # above, export _wants to_ obtain an identity FX graph (despite it
            # appears unnecessarily expensive for `torch.compile`), so we have
            # the following option to override Dynamo's preference for codegen
            # from source. Morever, this option applies recursively, for cases
            # like input tensor being returned in a new dictionary.
            #
            # And why the `ValueMutationExisting` check? Not sure, so leaving it
            # to keep the old behavior, as when `value_from_source` was
            # introduced. TODO sort out the invariants among side effect,
            # codegen and export.
            if (
                isinstance(value.mutation_type, ValueMutationExisting)
                or self.value_from_source
            ):
                return self(value.source)

        if value.is_python_constant() and is_safe_constant(value.as_python_constant()):
            output.append(self.create_load_const(value.as_python_constant()))
        elif isinstance(value, TensorWithTFOverrideVariable):
            graph_outputs_key = self.add_graph_output(value)

            self.add_push_null(
                lambda: self.load_import_from(utils.__name__, "to_subclass")
            )
            self.load_graph_output(graph_outputs[graph_outputs_key].index)
            output.append(
                self.create_load_global(
                    value.global_mangled_class_name(self.tx), add=True
                )
            )
            output.extend(create_call_function(2, False))
        elif (
            isinstance(value, SymNodeVariable)
            and value.python_type() == float
            and not self.tx.export
        ):
            # This is a little unusual; force the output convention to be a
            # Tensor here.  Don't do this for export because this is
            # apparently load bearing for export tests (but I am a bit
            # doubtful it actually works in the real world)
            # NB: It works to add_graph_output on a computed expression
            # as_tensor here, because we memoize as_tensor calls on
            # SymNodeVariable!
            graph_outputs_key = self.add_graph_output(
                value.as_tensor(self.tx, torch.float64)
            )

            def gen_fn():
                self.load_graph_output(graph_outputs[graph_outputs_key].index)
                output.append(self.create_load_attr("item"))

            self.add_push_null(gen_fn)
            output.extend(create_call_function(0, False))
        elif isinstance(
            value,
            (
                TensorVariable,
                SymNodeVariable,
                UnspecializedPythonVariable,
                NumpyNdarrayVariable,
            ),
        ):
            graph_outputs_key = self.add_graph_output(value)

            if isinstance(value, NumpyNdarrayVariable):
                self.add_push_null(
                    lambda: self.load_import_from(utils.__name__, "to_numpy_helper")
                )
                self.load_graph_output(graph_outputs[graph_outputs_key].index)
                output.extend(create_call_function(1, False))
            elif isinstance(value, UnspecializedPythonVariable) and value.need_unwrap:

                def gen_fn():
                    self.load_graph_output(graph_outputs[graph_outputs_key].index)
                    output.append(self.create_load_attr("item"))

                self.add_push_null(gen_fn)
                output.extend(create_call_function(0, False))
            else:
                self.load_graph_output(graph_outputs[graph_outputs_key].index)
        elif isinstance(value, NNModuleVariable):
            parts = value.module_key.split(".")
            if parts[0] in self.code_options["co_varnames"]:
                output.append(self.create_load(parts[0]))
                parts = parts[1:]
            else:
                assert self.root is not None
                output.append(self.create_load_const_unchecked(self.root))
            for part in parts:
                output.append(self.create_load_attr(part))
        else:
            self.uses[value] += 1
            try:
                self.call_reconstruct(value)
            except NotImplementedError:
                unimplemented(f"reconstruct: {value}")
            if allow_cache and value in self.tempvars:
                self._output.append(create_dup_top())
                self.add_cache(value)

        self.top_of_stack = value

    def add_graph_output(self, value):
        graph_outputs_key = id(value.as_proxy())
        if graph_outputs_key not in self.graph_outputs:
            self.graph_outputs[graph_outputs_key] = GraphOutputEntry(
                len(self.graph_outputs), value
            )
        return graph_outputs_key

    def load_graph_output(self, index):
        output = self._output
        output.append(self.create_load(self.graph_output_var))
        output.append(self.create_load_const(index))
        output.append(create_instruction("BINARY_SUBSCR"))

    def add_cache(self, value):
        var = self.new_var()
        self.tempvars[value] = var
        self._output.append(self.create_store(var))

    def foreach(self, items):
        for i in items:
            self(i)

    def setup_globally_cached(self, name, value):
        """Store value in a new global"""
        name = re.sub(r"[^a-zA-Z0-9_]+", "_", name)
        f_globals = self.tx.f_globals
        if name in f_globals:
            assert id(f_globals[name]) == id(value)
        else:
            f_globals[name] = value
        return [self.create_load_global(name, add=True)]

    def clear_tos(self):
        self.top_of_stack = None

    def append_output(self, inst):
        assert isinstance(inst, Instruction)
        self._output.append(inst)
        self.clear_tos()

    def extend_output(self, insts):
        assert all(isinstance(x, Instruction) for x in insts)
        self._output.extend(insts)
        self.clear_tos()

    def get_instructions(self) -> List[Instruction]:
        return self._output

    def create_load(self, name) -> Instruction:
        assert name in self.code_options["co_varnames"], f"{name} missing"
        return create_instruction("LOAD_FAST", argval=name)

    def create_load_closure(self, name) -> Instruction:
        assert name in self.cell_and_freevars()
        inst_name = "LOAD_FAST" if sys.version_info >= (3, 13) else "LOAD_CLOSURE"
        return create_instruction(inst_name, argval=name)

    def create_load_deref(self, name) -> Instruction:
        assert name in self.cell_and_freevars()
        return create_instruction("LOAD_DEREF", argval=name)

    def create_store(self, name) -> Instruction:
        assert name in self.code_options["co_varnames"], f"{name} missing"
        return create_instruction("STORE_FAST", argval=name)

    def create_store_deref(self, name) -> Instruction:
        assert name in self.cell_and_freevars()
        return create_instruction("STORE_DEREF", argval=name)

    def create_load_global(self, name, add=False) -> Instruction:
        if add:
            self.tx.output.update_co_names(name)
        assert name in self.code_options["co_names"], f"{name} not in co_names"
        return create_instruction("LOAD_GLOBAL", argval=name)

    def create_load_const(self, value) -> Instruction:
        return create_load_const(value)

    def create_load_const_unchecked(self, value) -> Instruction:
        return create_load_const(value, checked=False)

    def load_method(self, name):
        self.tx.output.update_co_names(name)
        self.append_output(create_load_method(name))

    def call_method(self, nargs):
        self.extend_output(create_call_method(nargs))

    def create_load_attr(self, name) -> Instruction:
        if name not in self.code_options["co_names"]:
            self.code_options["co_names"] += (name,)
        return create_instruction("LOAD_ATTR", argval=name)

    def load_attr(self, name):
        self.append_output(self.create_load_attr(name))

    def create_load_attrs(self, names):
        return [self.create_load_attr(name) for name in names.split(".")]

    def create_store_attr(self, name) -> Instruction:
        if name not in self.code_options["co_names"]:
            self.code_options["co_names"] += (name,)
        return create_instruction("STORE_ATTR", argval=name)

    def store_attr(self, name):
        self.append_output(self.create_store_attr(name))

    def load_function_name(self, fn_name, push_null, num_on_stack=0):
        """Load the global fn_name on the stack num_on_stack down"""
        output = []
        if push_null and sys.version_info >= (3, 11):
            output.extend(add_push_null(self.create_load_global(fn_name, add=True)))
            if num_on_stack > 0:
                output.extend(
                    [
                        *self.rot_n(num_on_stack + 2),
                        *self.rot_n(num_on_stack + 2),
                    ]
                )
        else:
            output.extend(
                [
                    self.create_load_global(fn_name, add=True),
                    *self.rot_n(num_on_stack + 1),
                ]
            )
        return output

    def rot_n(self, n):
        try:
            return create_rot_n(n)
        except AttributeError:
            # desired rotate bytecode doesn't exist, generate equivalent bytecode
            return [
                create_instruction("BUILD_TUPLE", arg=n),
                self.create_load_const_unchecked(rot_n_helper(n)),
                *create_rot_n(2),
                create_instruction("CALL_FUNCTION_EX", arg=0),
                create_instruction("UNPACK_SEQUENCE", arg=n),
            ]

    def pop_null(self):
        # POP_TOP doesn't work for null, so we pop nulls by pushing in a
        # nop function, calling it (which consumes the null), and popping the result.
        assert sys.version_info >= (3, 11)
        return [
            self.create_load_const_unchecked(lambda: None),
            # 3.13 swapped NULL and callable
            *(
                (create_instruction("SWAP", arg=2),)
                if sys.version_info >= (3, 13)
                else ()
            ),
            *create_call_function(0, False),
            create_instruction("POP_TOP"),
        ]

    def pop_top(self):
        self.append_output(create_instruction("POP_TOP"))

    def call_function(self, nargs: int, push_null: bool):
        self.extend_output(create_call_function(nargs, push_null=push_null))

    def dup_top(self):
        self.append_output(create_dup_top())

    def store(self, varname):
        self.append_output(self.create_store(varname))

    def load_deref(self, varname):
        self.append_output(self.create_load_deref(varname))

    def make_function_with_closure(
        self, fn_name: str, code: types.CodeType, push_null: bool, num_on_stack=0
    ):
        freevars = code.co_freevars
        assert freevars
        output = self._output

        def gen_fn():
            # Emitting `LOAD_FAST/LOAD_CLOSURE` with names in `co_freevars`
            # requires that in the generated bytecode, these cells would keep
            # their original local names, which we ensure via
            # `CellVariable.local_name`.
            for var in freevars:
                assert var in self.cell_and_freevars()
                output.append(self.create_load_closure(var))
            output.append(create_instruction("BUILD_TUPLE", arg=len(freevars)))
            output.append(self.create_load_const(code))
            if sys.version_info < (3, 11):
                output.append(self.create_load_const(fn_name))
            if sys.version_info >= (3, 13):
                output.extend(
                    [
                        create_instruction("MAKE_FUNCTION"),
                        create_instruction("SET_FUNCTION_ATTRIBUTE", arg=0x08),
                    ]
                )
            else:
                output.append(create_instruction("MAKE_FUNCTION", arg=0x08))

        if push_null and sys.version_info >= (3, 11):
            self.add_push_null(gen_fn)
            output.extend(self.rot_n(num_on_stack + 2))
            output.extend(self.rot_n(num_on_stack + 2))
        else:
            gen_fn()
            output.extend(self.rot_n(num_on_stack + 1))
        self.clear_tos()

    def create_load_python_module(self, mod) -> Instruction:
        """
        Generate a LOAD_GLOBAL instruction to fetch a given python module.
        """
        output = self.tx.output
        global_scope = output.global_scope
        name = re.sub(r"^.*[.]", "", mod.__name__)
        if global_scope.get(name, None) is mod:
            return self.create_load_global(name, add=True)
        prefix = f"___module_{name}"
        global_name = self.tx.output.install_global_by_id(prefix, mod)
        return self.create_load_global(global_name, add=True)

    def make_call_generated_code(self, fn_name: str) -> None:
        """Call the generated code function stored in fn_name"""
        self.extend_output(self.load_function_name(fn_name, True))

        graphargs = self.tx.output.graphargs
        for arg in graphargs:
            if arg.pass_arg_as_tensor:
                self.add_push_null(
                    lambda: self.extend_output(
                        [
                            self.create_load_python_module(torch),
                            self.create_load_attr("_as_tensor_fullprec"),
                        ]
                    )
                )
                self.call_reconstruct(arg)
                self.extend_output(create_call_function(1, False))
            else:
                self.call_reconstruct(arg)

        self.extend_output(create_call_function(len(graphargs), False))

    def load_import_from(self, module_name, object_name) -> None:
        self(AttrSource(self.tx.import_source(module_name), object_name))

    def create_call_function_kw(self, nargs, kw_names, push_null) -> List[Instruction]:
        if sys.version_info >= (3, 13):
            output = create_call_function(nargs, push_null)
            assert output[-1].opname == "CALL"
            output.insert(-1, self.create_load_const(kw_names))
            output[-1] = create_instruction("CALL_KW", arg=nargs)
            return output
        elif sys.version_info >= (3, 11):
            output = create_call_function(nargs, push_null)
            if sys.version_info >= (3, 12):
                idx = -1
                expected_inst = "CALL"
            else:
                idx = -2
                expected_inst = "PRECALL"
            assert output[idx].opname == expected_inst
            kw_names_inst = create_instruction("KW_NAMES", argval=kw_names)
            output.insert(idx, kw_names_inst)
            return output
        return [
            self.create_load_const(kw_names),
            create_instruction("CALL_FUNCTION_KW", arg=nargs),
        ]

    def create_delete(self, value) -> Instruction:
        return create_instruction("DELETE_FAST", argval=value)
