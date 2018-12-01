from abc import abstractproperty

from jedi import debug
from jedi import settings
from jedi.evaluate import compiled
from jedi.evaluate import filters
from jedi.evaluate.base_context import Context, NO_CONTEXTS, ContextSet, \
    iterator_to_context_set, ContextWrapper
from jedi.evaluate.lazy_context import LazyKnownContext, LazyKnownContexts
from jedi.evaluate.cache import evaluator_method_cache
from jedi.evaluate.arguments import AnonymousArguments, \
    ValuesArguments, TreeArgumentsWrapper
from jedi.evaluate.context.function import FunctionExecutionContext, \
    FunctionContext, FunctionMixin, OverloadedFunctionContext
from jedi.evaluate.context.klass import ClassContext, apply_py__get__, \
    ClassFilter
from jedi.evaluate.context import iterable
from jedi.parser_utils import get_parent_scope


class InstanceExecutedParam(object):
    def __init__(self, instance, tree_param):
        self._instance = instance
        self._tree_param = tree_param
        self.string_name = self._tree_param.name.value

    def infer(self):
        return ContextSet([self._instance])

    def matches_signature(self):
        return True


class AnonymousInstanceArguments(AnonymousArguments):
    def __init__(self, instance):
        self._instance = instance

    def get_executed_params_and_issues(self, execution_context):
        from jedi.evaluate.dynamic import search_params
        tree_params = execution_context.tree_node.get_params()
        if not tree_params:
            return [], []

        self_param = InstanceExecutedParam(self._instance, tree_params[0])
        if len(tree_params) == 1:
            # If the only param is self, we don't need to try to find
            # executions of this function, we have all the params already.
            return [self_param], []
        executed_params = list(search_params(
            execution_context.evaluator,
            execution_context,
            execution_context.tree_node
        ))
        executed_params[0] = self_param
        return executed_params, []


class AbstractInstanceContext(Context):
    """
    This class is used to evaluate instances.
    """
    api_type = u'instance'

    def __init__(self, evaluator, parent_context, class_context, var_args):
        super(AbstractInstanceContext, self).__init__(evaluator, parent_context)
        # Generated instances are classes that are just generated by self
        # (No var_args) used.
        self.class_context = class_context
        self.var_args = var_args

    def is_instance(self):
        return True

    def get_annotated_class_object(self):
        return self.class_context  # This is the default.

    @property
    def py__call__(self):
        names = self.get_function_slot_names(u'__call__')
        if not names:
            # Means the Instance is not callable.
            raise AttributeError

        def execute(arguments):
            return ContextSet.from_sets(name.infer().execute(arguments) for name in names)

        return execute

    def py__class__(self):
        return self.class_context

    def py__bool__(self):
        # Signalize that we don't know about the bool type.
        return None

    def get_function_slot_names(self, name):
        # Python classes don't look at the dictionary of the instance when
        # looking up `__call__`. This is something that has to do with Python's
        # internal slot system (note: not __slots__, but C slots).
        for filter in self.get_filters(include_self_names=False):
            names = filter.get(name)
            if names:
                return names
        return []

    def execute_function_slots(self, names, *evaluated_args):
        return ContextSet.from_sets(
            name.infer().execute_evaluated(*evaluated_args)
            for name in names
        )

    def py__get__(self, obj, class_context):
        """
        obj may be None.
        """
        # Arguments in __get__ descriptors are obj, class.
        # `method` is the new parent of the array, don't know if that's good.
        names = self.get_function_slot_names(u'__get__')
        if names:
            if obj is None:
                obj = compiled.builtin_from_name(self.evaluator, u'None')
            return self.execute_function_slots(names, obj, class_context)
        else:
            return ContextSet([self])

    def get_filters(self, search_global=None, until_position=None,
                    origin_scope=None, include_self_names=True):
        class_context = self.get_annotated_class_object()
        if include_self_names:
            for cls in class_context.py__mro__():
                if not isinstance(cls, compiled.CompiledObject) \
                        or cls.tree_node is not None:
                    # In this case we're excluding compiled objects that are
                    # not fake objects. It doesn't make sense for normal
                    # compiled objects to search for self variables.
                    yield SelfAttributeFilter(self.evaluator, self, cls, origin_scope)

        for cls in class_context.py__mro__():
            if isinstance(cls, compiled.CompiledObject):
                yield CompiledInstanceClassFilter(self.evaluator, self, cls)
            else:
                yield InstanceClassFilter(self.evaluator, self, cls, origin_scope)

    def py__getitem__(self, index_context_set, contextualized_node):
        names = self.get_function_slot_names(u'__getitem__')
        if not names:
            debug.warning('Found no __getitem__ on %s', self)
            return NO_CONTEXTS

        args = ValuesArguments([index_context_set])
        return ContextSet.from_sets(name.infer().execute(args) for name in names)

    def py__iter__(self, contextualized_node=None):
        iter_slot_names = self.get_function_slot_names(u'__iter__')
        if not iter_slot_names:
            return super(AbstractInstanceContext, self).py__iter__(contextualized_node)

        for generator in self.execute_function_slots(iter_slot_names):
            if generator.is_instance():
                # `__next__` logic.
                if self.evaluator.environment.version_info.major == 2:
                    name = u'next'
                else:
                    name = u'__next__'
                iter_slot_names = generator.get_function_slot_names(name)
                if iter_slot_names:
                    yield LazyKnownContexts(
                        generator.execute_function_slots(iter_slot_names)
                    )
                else:
                    debug.warning('Instance has no __next__ function in %s.', generator)
            else:
                for lazy_context in generator.py__iter__():
                    yield lazy_context

    @abstractproperty
    def name(self):
        pass

    def create_init_executions(self):
        for name in self.get_function_slot_names(u'__init__'):
            # TODO is this correct? I think we need to check for functions.
            if isinstance(name, LazyInstanceClassName):
                function = FunctionContext.from_context(
                    self.parent_context,
                    name.tree_name.parent
                )
                bound_method = BoundMethod(self, function)
                yield bound_method.get_function_execution(self.var_args)

    @evaluator_method_cache()
    def create_instance_context(self, class_context, node):
        if node.parent.type in ('funcdef', 'classdef'):
            node = node.parent
        scope = get_parent_scope(node)
        if scope == class_context.tree_node:
            return class_context
        else:
            parent_context = self.create_instance_context(class_context, scope)
            if scope.type == 'funcdef':
                func = FunctionContext.from_context(
                    parent_context,
                    scope,
                )
                bound_method = BoundMethod(self, func)
                if scope.name.value == '__init__' and parent_context == class_context:
                    return bound_method.get_function_execution(self.var_args)
                else:
                    return bound_method.get_function_execution()
            elif scope.type == 'classdef':
                class_context = ClassContext(self.evaluator, parent_context, scope)
                return class_context
            elif scope.type == 'comp_for':
                # Comprehensions currently don't have a special scope in Jedi.
                return self.create_instance_context(class_context, scope)
            else:
                raise NotImplementedError
        return class_context

    def get_signatures(self):
        init_funcs = self.py__getattribute__('__call__')
        return [sig.bind(self) for sig in init_funcs.get_signatures()]

    def __repr__(self):
        return "<%s of %s(%s)>" % (self.__class__.__name__, self.class_context,
                                   self.var_args)


class CompiledInstance(AbstractInstanceContext):
    def __init__(self, evaluator, parent_context, class_context, var_args):
        self._original_var_args = var_args
        super(CompiledInstance, self).__init__(evaluator, parent_context, class_context, var_args)

    @property
    def name(self):
        return compiled.CompiledContextName(self, self.class_context.name.string_name)

    def create_instance_context(self, class_context, node):
        if get_parent_scope(node).type == 'classdef':
            return class_context
        else:
            return super(CompiledInstance, self).create_instance_context(class_context, node)

    def get_first_non_keyword_argument_contexts(self):
        key, lazy_context = next(self._original_var_args.unpack(), ('', None))
        if key is not None:
            return NO_CONTEXTS

        return lazy_context.infer()


class TreeInstance(AbstractInstanceContext):
    def __init__(self, evaluator, parent_context, class_context, var_args):
        # I don't think that dynamic append lookups should happen here. That
        # sounds more like something that should go to py__iter__.
        if class_context.py__name__() in ['list', 'set'] \
                and parent_context.get_root_context() == evaluator.builtins_module:
            # compare the module path with the builtin name.
            if settings.dynamic_array_additions:
                var_args = iterable.get_dynamic_array_instance(self, var_args)

        super(TreeInstance, self).__init__(evaluator, parent_context,
                                           class_context, var_args)
        self.tree_node = class_context.tree_node

    @property
    def name(self):
        return filters.ContextName(self, self.class_context.name.tree_name)

    # This can recurse, if the initialization of the class includes a reference
    # to itself.
    @evaluator_method_cache(default=None)
    def _get_annotated_class_object(self):
        from jedi.evaluate import pep0484

        for func in self._get_annotation_init_functions():
            # Just take the first result, it should always be one, because we
            # control the typeshed code.
            bound = BoundMethod(self, func)
            execution = bound.get_function_execution(self.var_args)
            if not execution.matches_signature():
                # First check if the signature even matches, if not we don't
                # need to infer anything.
                continue

            all_annotations = pep0484.py__annotations__(execution.tree_node)
            defined = self.class_context.define_generics(
                pep0484.infer_type_vars_for_execution(execution, all_annotations),
            )
            debug.dbg('Inferred instance context as %s', defined, color='BLUE')
            return defined
        return None

    def get_annotated_class_object(self):
        return self._get_annotated_class_object() or self.class_context

    def _get_annotation_init_functions(self):
        filter = next(self.class_context.get_filters())
        for init_name in filter.get('__init__'):
            for init in init_name.infer():
                if init.is_function():
                    for signature in init.get_signatures():
                        yield signature.context


class AnonymousInstance(TreeInstance):
    def __init__(self, evaluator, parent_context, class_context):
        super(AnonymousInstance, self).__init__(
            evaluator,
            parent_context,
            class_context,
            var_args=AnonymousInstanceArguments(self),
        )

    def get_annotated_class_object(self):
        return self.class_context  # This is the default.


class CompiledInstanceName(compiled.CompiledName):

    def __init__(self, evaluator, instance, klass, name):
        super(CompiledInstanceName, self).__init__(
            evaluator,
            klass.parent_context,
            name.string_name
        )
        self._instance = instance
        self._class = klass
        self._class_member_name = name

    @iterator_to_context_set
    def infer(self):
        for result_context in self._class_member_name.infer():
            if result_context.api_type == 'function':
                yield CompiledBoundMethod(result_context)
            else:
                yield result_context


class CompiledInstanceClassFilter(filters.AbstractFilter):
    name_class = CompiledInstanceName

    def __init__(self, evaluator, instance, klass):
        self._evaluator = evaluator
        self._instance = instance
        self._class = klass
        self._class_filter = next(klass.get_filters(is_instance=True))

    def get(self, name):
        return self._convert(self._class_filter.get(name))

    def values(self):
        return self._convert(self._class_filter.values())

    def _convert(self, names):
        return [
            CompiledInstanceName(self._evaluator, self._instance, self._class, n)
            for n in names
        ]


class BoundMethod(FunctionMixin, ContextWrapper):
    def __init__(self, instance, function):
        super(BoundMethod, self).__init__(function)
        self.instance = instance

    def py__class__(self):
        return compiled.get_special_object(self.evaluator, u'BOUND_METHOD_CLASS')

    def _get_arguments(self, arguments):
        if arguments is None:
            arguments = AnonymousInstanceArguments(self.instance)

        return InstanceArguments(self.instance, arguments)

    def get_function_execution(self, arguments=None):
        arguments = self._get_arguments(arguments)

        if isinstance(self._wrapped_context, compiled.CompiledObject):
            # This is kind of weird, because it's coming from a compiled object
            # and we're not sure if we want that in the future.
            # TODO remove?!
            return FunctionExecutionContext(
                self.evaluator, self.parent_context, self, arguments
            )

        return super(BoundMethod, self).get_function_execution(arguments)

    def py__call__(self, arguments):
        if isinstance(self._wrapped_context, OverloadedFunctionContext):
            return self._wrapped_context.py__call__(self._get_arguments(arguments))

        # This might not be the most beautiful way, but prefer stub_contexts
        # and execute those if possible.
        try:
            stub_context = self._wrapped_context.stub_context
        except AttributeError:
            pass
        else:
            return stub_context.py__call__(arguments)

        function_execution = self.get_function_execution(arguments)
        return function_execution.infer()

    def get_matching_functions(self, arguments):
        for func in self._wrapped_context.get_matching_functions(arguments):
            if func is self:
                yield self
            else:
                yield BoundMethod(self.instance, func)

    def get_signatures(self):
        return [sig.bind(self) for sig in self._wrapped_context.get_signatures()]

    def __repr__(self):
        return '<%s: %s>' % (self.__class__.__name__, self._wrapped_context)


class CompiledBoundMethod(compiled.CompiledObject):
    def __init__(self, func):
        super(CompiledBoundMethod, self).__init__(
            func.evaluator, func.access_handle, func.parent_context, func.tree_node)

    def get_param_names(self):
        return list(super(CompiledBoundMethod, self).get_param_names())[1:]


class SelfName(filters.TreeNameDefinition):
    """
    This name calculates the parent_context lazily.
    """
    def __init__(self, instance, class_context, tree_name):
        self._instance = instance
        self.class_context = class_context
        self.tree_name = tree_name

    @property
    def parent_context(self):
        return self._instance.create_instance_context(self.class_context, self.tree_name)


class LazyInstanceClassName(object):
    def __init__(self, instance, class_context, class_member_name):
        self._instance = instance
        self.class_context = class_context
        self._class_member_name = class_member_name

    @iterator_to_context_set
    def infer(self):
        for result_context in self._class_member_name.infer():
            for c in apply_py__get__(result_context, self._instance, self.class_context):
                yield c

    def __getattr__(self, name):
        return getattr(self._class_member_name, name)

    def __repr__(self):
        return '<%s: %s>' % (self.__class__.__name__, self._class_member_name)


class InstanceClassFilter(filters.AbstractFilter):
    """
    This filter is special in that it uses the class filter and wraps the
    resulting names in LazyINstanceClassName. The idea is that the class name
    filtering can be very flexible and always be reflected in instances.
    """
    def __init__(self, evaluator, context, class_context, origin_scope):
        self._instance = context
        self._class_context = class_context
        self._class_filter = next(class_context.get_filters(
            search_global=False,
            origin_scope=origin_scope,
            is_instance=True,
        ))

    def get(self, name):
        return self._convert(self._class_filter.get(name))

    def values(self):
        return self._convert(self._class_filter.values())

    def _convert(self, names):
        return [LazyInstanceClassName(self._instance, self._class_context, n) for n in names]

    def __repr__(self):
        return '<%s for %s>' % (self.__class__.__name__, self._class_context)


class SelfAttributeFilter(ClassFilter):
    """
    This class basically filters all the use cases where `self.*` was assigned.
    """
    name_class = SelfName

    def __init__(self, evaluator, context, class_context, origin_scope):
        super(SelfAttributeFilter, self).__init__(
            evaluator=evaluator,
            context=context,
            node_context=class_context,
            origin_scope=origin_scope,
            is_instance=True,
        )
        self._class_context = class_context

    def _filter(self, names):
        names = self._filter_self_names(names)
        if isinstance(self._parser_scope, compiled.CompiledObject) and False:
            # This would be for builtin skeletons, which are not yet supported.
            return list(names)
        else:
            start, end = self._parser_scope.start_pos, self._parser_scope.end_pos
            return [n for n in names if start < n.start_pos < end]

    def _filter_self_names(self, names):
        for name in names:
            trailer = name.parent
            if trailer.type == 'trailer' \
                    and len(trailer.children) == 2 \
                    and trailer.children[0] == '.':
                if name.is_definition() and self._access_possible(name):
                    yield name

    def _convert_names(self, names):
        return [self.name_class(self.context, self._class_context, name) for name in names]

    def _check_flows(self, names):
        return names


class InstanceArguments(TreeArgumentsWrapper):
    def __init__(self, instance, arguments):
        super(InstanceArguments, self).__init__(arguments)
        self.instance = instance

    def unpack(self, func=None):
        yield None, LazyKnownContext(self.instance)
        for values in self._wrapped_arguments.unpack(func):
            yield values

    def get_executed_params_and_issues(self, execution_context):
        if isinstance(self._wrapped_arguments, AnonymousInstanceArguments):
            return self._wrapped_arguments.get_executed_params_and_issues(execution_context)

        return super(InstanceArguments, self).get_executed_params_and_issues(execution_context)
