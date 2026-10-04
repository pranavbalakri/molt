"""Tests for molt's tail call elimination (TAIL_CALL, TAIL_CALL_KW, TAIL_CALL_EX)."""

import _thread
import dis
import enum
import faulthandler
import io
import os
import re
import sys
import tempfile
import threading
import traceback
import unittest
import warnings
import weakref
from test import support
from test.support import import_helper, script_helper


TCE_DISABLED = (
    "notce" in sys._xoptions
    or (os.environ.get("PYTHONNOTCE") and not sys.flags.ignore_environment)
    or support.Py_GIL_DISABLED
)
requires_tce = unittest.skipIf(TCE_DISABLED, "tail call elimination is disabled")

TAIL_OPS = {"TAIL_CALL", "TAIL_CALL_KW", "TAIL_CALL_EX"}
CALL_OPS = {"CALL", "CALL_KW", "CALL_FUNCTION_EX"}


def call_opnames(func):
    return [i.opname for i in dis.get_instructions(func)
            if i.opname in TAIL_OPS | CALL_OPS]


def caller_name():
    """Name of the function that called the caller of caller_name().

    Must not be called in tail position, or the frame it is asked about
    is the one that gets replaced.
    """
    name = sys._getframe(2).f_code.co_name
    return name


def frames_at_bottom(n):
    """Recurse n times, then count the frames_at_bottom frames on the stack."""
    if n == 0:
        return sum(f.name == "frames_at_bottom" for f in traceback.extract_stack())
    return frames_at_bottom(n - 1)


def count(n, acc=0):
    if n == 0:
        return acc
    return count(n - 1, acc + 1)


def raise_after(n):
    if n == 0:
        raise ValueError("bottom")
    return raise_after(n - 1)


def tail_calls_at_bottom(n):
    if n == 0:
        return sys._getframe().f_tail_calls
    return tail_calls_at_bottom(n - 1)


def is_even(n):
    if n == 0:
        return True
    return is_odd(n - 1)


def is_odd(n):
    if n == 0:
        return False
    return is_even(n - 1)


class CompilerTests(unittest.TestCase):

    def test_tail_positions(self):
        def positional(n):
            return count(n)
        def keywords(n):
            return count(n, acc=1)
        def star(args, kwargs):
            return count(*args, **kwargs)
        def method(obj):
            return obj.method(1)
        def in_loop(xs):
            for x in xs:
                while x:
                    return count(x)
        lam = lambda n: count(n)
        self.assertEqual(call_opnames(positional), ["TAIL_CALL"])
        self.assertEqual(call_opnames(keywords), ["TAIL_CALL_KW"])
        self.assertEqual(call_opnames(star), ["TAIL_CALL_EX"])
        self.assertEqual(call_opnames(method), ["TAIL_CALL"])
        self.assertEqual(call_opnames(in_loop), ["TAIL_CALL"])
        self.assertEqual(call_opnames(lam), ["TAIL_CALL"])

    def test_tail_call_is_followed_by_return(self):
        def f(n):
            return count(n)
        names = [i.opname for i in dis.get_instructions(f)]
        self.assertEqual(names[-2:], ["TAIL_CALL", "RETURN_VALUE"])

    def test_only_outermost_call_is_tail(self):
        def f(x):
            return count(len(x))
        self.assertEqual(call_opnames(f), ["CALL", "TAIL_CALL"])

    def test_not_tail_positions(self):
        def not_tail(n):
            return 1 + count(n)
        def in_try_finally(n):
            try:
                return count(n)
            finally:
                pass
        def in_try_except(n):
            try:
                return count(n)
            except ValueError:
                pass
        def in_except_handler(n):
            try:
                pass
            except ValueError:
                return count(n)
        def in_with(cm, n):
            with cm:
                return count(n)
        def generator(n):
            yield
            return count(n)
        async def coroutine(n):
            return count(n)
        async def async_generator(n):
            yield count(n)
        async def in_async_with(cm, n):
            async with cm:
                return count(n)
        for func in (not_tail, in_try_finally, in_try_except,
                     in_except_handler, in_with, generator, coroutine,
                     async_generator, in_async_with):
            with self.subTest(func=func.__name__):
                self.assertFalse(TAIL_OPS & set(call_opnames(func)))

    def test_module_and_class_bodies(self):
        code = compile("count(1)\nclass C:\n    x = count(1)\n", "<test>", "exec")
        for co in [code] + [c for c in code.co_consts if hasattr(c, "co_code")]:
            ops = {i.opname for i in dis.get_instructions(co)}
            self.assertFalse(TAIL_OPS & ops, co)
        self.assertFalse(TAIL_OPS & set(call_opnames(
            compile("count(1)", "<test>", "eval"))))


@requires_tce
class EliminationTests(unittest.TestCase):

    @support.no_tracing
    def test_self_recursion_10_million_deep(self):
        self.assertEqual(sys.getrecursionlimit(), 1000)
        self.assertEqual(count(10_000_000), 10_000_000)

    @support.no_tracing
    def test_mutual_recursion_1_million_deep(self):
        self.assertTrue(is_even(1_000_000))
        self.assertFalse(is_even(1_000_001))
        self.assertTrue(is_odd(1_000_001))

    @support.no_tracing
    def test_keywords_and_defaults(self):
        def f(n, *, acc=0, step=1):
            if n == 0:
                return acc
            return f(n - 1, step=step, acc=acc + step)
        self.assertEqual(f(100_000), 100_000)
        self.assertEqual(f(100_000, step=3), 300_000)

    @support.no_tracing
    def test_varargs_and_varkw(self):
        def f(n, *args, **kwargs):
            if n == 0:
                return args, kwargs
            return f(n - 1, *args, **kwargs)
        self.assertEqual(f(100_000, 1, 2, x=3), ((1, 2), {"x": 3}))

        def g(n, *args, **kwargs):
            if n == 0:
                return len(args), sorted(kwargs)
            return g(n - 1, *args, n, **kwargs, **{f"k{n % 3}": n})
        self.assertEqual(g(3, a=1), (3, ["a", "k0", "k1", "k2"]))

    @support.no_tracing
    def test_closure(self):
        def make(limit):
            def loop(n):
                if n == limit:
                    return n
                return loop(n + 1)
            return loop
        self.assertEqual(make(100_000)(0), 100_000)

    @support.no_tracing
    def test_lambda(self):
        # A lambda body is in tail position.
        step = lambda n: down(n - 1)
        def down(n):
            if n == 0:
                return "done"
            return step(n)
        self.assertEqual(down(100_000), "done")

    @support.no_tracing
    def test_methods(self):
        class C:
            def method(self, n):
                if n == 0:
                    return self
                return self.method(n - 1)
            @staticmethod
            def static(n):
                if n == 0:
                    return "static"
                return C.static(n - 1)
            @classmethod
            def klass(cls, n):
                if n == 0:
                    return cls
                return cls.klass(n - 1)
        obj = C()
        self.assertIs(obj.method(100_000), obj)
        self.assertEqual(C.static(100_000), "static")
        self.assertIs(C.klass(100_000), C)

    @support.no_tracing
    def test_bound_method_object(self):
        class C:
            def down(self, n):
                if n == 0:
                    return "done"
                return bound(n - 1)
        bound = C().down
        self.assertEqual(bound(100_000), "done")

    def test_frame_is_replaced(self):
        def target():
            name = caller_name()
            return name
        def middle():
            return target()
        def outer():
            name = middle()
            return name
        # middle's frame is replaced by target's, so target's caller is outer.
        self.assertEqual(outer(), "outer")
        self.assertEqual(frames_at_bottom(20), 1)

    @support.no_tracing
    def test_called_from_c(self):
        # The frame being replaced was called from C (map) rather than from
        # Python, so the new frame must return to the C caller.
        self.assertEqual(list(map(count, [100_000, 5])), [100_000, 5])
        self.assertEqual(sorted([3, 1, 2], key=lambda x: count(x)), [1, 2, 3])

    @support.no_tracing
    def test_exception_propagates(self):
        def down(n):
            if n == 0:
                raise ValueError("bottom")
            return down(n - 1)
        def caller():
            try:
                down(100_000)
            except ValueError as exc:
                return exc
        exc = caller()
        self.assertIsInstance(exc, ValueError)
        self.assertEqual(str(exc), "bottom")
        names = [f.name for f in traceback.extract_tb(exc.__traceback__)]
        self.assertEqual(names, ["caller", "down"])

    def test_exception_caught_by_intermediate_frame(self):
        def down(n):
            if n == 0:
                raise KeyError(n)
            return down(n - 1)
        def guard(n):
            try:
                return down(n)
            except KeyError:
                return "caught"
        def start(n):
            return guard(n)
        self.assertEqual(start(1000), "caught")

    def test_bad_arguments_keep_calling_frame(self):
        def two(a, b):
            return a + b
        def bad():
            return two(1, 2, 3)
        try:
            bad()
        except TypeError as exc:
            names = [f.name for f in traceback.extract_tb(exc.__traceback__)]
        else:
            self.fail("TypeError not raised")
        self.assertEqual(names[-1], "bad")

    def test_locals_released_before_callee_runs(self):
        class Tracked:
            pass
        def holder():
            obj = Tracked()
            nonlocal ref
            ref = weakref.ref(obj)
            return check()
        def check():
            return ref() is None
        ref = None
        self.assertTrue(holder())

    def test_infinite_tail_recursion_is_interruptible(self):
        def forever():
            return forever()
        timer = threading.Timer(0.5, _thread.interrupt_main)
        timer.start()
        try:
            with self.assertRaises(KeyboardInterrupt):
                forever()
        finally:
            timer.cancel()


@requires_tce
class CounterTests(unittest.TestCase):

    def test_frame_counter(self):
        self.assertEqual(sys._getframe().f_tail_calls, 0)
        self.assertEqual(tail_calls_at_bottom(0), 0)
        self.assertEqual(tail_calls_at_bottom(7), 7)

    def test_counter_survives_frame_object_copy(self):
        def bottom(n):
            if n == 0:
                return sys._getframe()
            return bottom(n - 1)
        # The frame object outlives its frame, so it gets a copy of it.
        self.assertEqual(bottom(3).f_tail_calls, 3)

    def test_thread_counter(self):
        before = sys._tail_calls_eliminated()
        tail_calls_at_bottom(100)
        self.assertEqual(sys._tail_calls_eliminated() - before, 100)

        def worker():
            results.append(sys._tail_calls_eliminated())
            tail_calls_at_bottom(10_000)
            results.append(sys._tail_calls_eliminated())
        results = []
        before = sys._tail_calls_eliminated()
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        self.assertEqual(results[1] - results[0], 10_000)
        # Starting and joining the thread may make a few tail calls here,
        # but the worker's are counted only in the worker.
        self.assertLess(sys._tail_calls_eliminated() - before, 10_000)


@requires_tce
class FrameDepthTests(unittest.TestCase):
    """Lookups by depth count the frames that tail calls eliminated."""

    def test_getframe(self):
        def inner(depth):
            frame = sys._getframe(depth)
            return frame.f_code.co_name
        def middle(depth):
            return inner(depth)
        def outer(depth):
            name = middle(depth)
            return name
        self.assertEqual(outer(0), "inner")
        # Level 1 is middle, which was eliminated; the frame above it is used.
        self.assertEqual(outer(1), "outer")
        self.assertEqual(outer(2), "outer")
        self.assertEqual(outer(3), "test_getframe")

    def test_getframemodulename(self):
        # EnumType.__call__ tail-calls _create_, which looks two frames up.
        Color = enum.Enum("Color", "RED GREEN")
        self.assertEqual(Color.__module__, __name__)

    def test_warning_stacklevel(self):
        def inner():
            warnings.warn("deep", UserWarning, stacklevel=3)
        def middle():
            return inner()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            middle(); lineno = sys._getframe().f_lineno
        self.assertEqual(caught[0].filename, __file__)
        self.assertEqual(caught[0].lineno, lineno)

    def test_warning_stacklevel_pure_python(self):
        code = "\n".join([
            "import sys",
            "sys.modules.pop('warnings', None)",
            "sys.modules['_warnings'] = None",
            "import warnings",
            "def inner():",
            "    warnings.warn('deep', UserWarning, stacklevel=3)",
            "def middle():",
            "    return inner()",
            "with warnings.catch_warnings(record=True) as caught:",
            "    warnings.simplefilter('always')",
            "    middle()",
            "print(warnings.warn.__module__, caught[0].lineno)",
        ])
        res = script_helper.assert_python_ok("-c", code)
        self.assertEqual(res.out.decode().split(), ["_py_warnings", "11"])

    def test_re_warning_location(self):
        re.purge()
        with self.assertWarns(FutureWarning) as cm:
            re.compile(r"[[a-y]]")
        self.assertEqual(cm.filename, __file__)


@requires_tce
class TracebackAnnotationTests(unittest.TestCase):

    def get_exception(self, n):
        try:
            raise_after(n)
        except ValueError as exc:
            return exc

    def test_traceback_module(self):
        lines = traceback.format_exception(self.get_exception(5))
        index = lines.index("  [5 tail calls eliminated]\n")
        self.assertIn("in get_exception", lines[index - 1])
        self.assertIn("in raise_after", lines[index + 1])

    def test_singular(self):
        lines = traceback.format_exception(self.get_exception(1))
        self.assertIn("  [1 tail call eliminated]\n", lines)

    def test_no_annotation_without_tail_calls(self):
        lines = traceback.format_exception(self.get_exception(0))
        self.assertFalse([line for line in lines if "eliminated" in line])

    def test_c_traceback_printer(self):
        _testcapi = import_helper.import_module("_testcapi")
        output = io.StringIO()
        _testcapi.traceback_print(self.get_exception(4).__traceback__, output)
        lines = output.getvalue().splitlines()
        index = lines.index("  [4 tail calls eliminated]")
        self.assertIn("in raise_after", lines[index + 1])

    def test_format_stack(self):
        def bottom(n):
            if n == 0:
                stack = traceback.format_stack()
                return stack
            return bottom(n - 1)
        stack = bottom(3)
        self.assertEqual(stack[-2], "  [3 tail calls eliminated]\n")
        self.assertIn("in bottom", stack[-1])

    def test_faulthandler(self):
        def bottom(n):
            if n == 0:
                with tempfile.TemporaryFile("w+") as fp:
                    faulthandler.dump_traceback(fp, all_threads=False)
                    fp.seek(0)
                    lines = fp.read().splitlines()
                return lines
            return bottom(n - 1)
        lines = bottom(2)
        # Most recent call first: the annotation follows the frame.
        index = lines.index("  [2 tail calls eliminated]")
        self.assertIn("in bottom", lines[index - 1])

    def test_uncaught_exception(self):
        code = (
            "def down(n):\n"
            "    if n == 0:\n"
            "        raise KeyError(n)\n"
            "    return down(n - 1)\n"
            "down(3)\n"
        )
        res = script_helper.assert_python_failure("-c", code)
        self.assertIn(b"  [3 tail calls eliminated]\n", res.err)
        res = script_helper.assert_python_failure("-X", "notce", "-c", code)
        self.assertNotIn(b"eliminated", res.err)


class NotEliminatedTests(unittest.TestCase):

    def test_builtin(self):
        def f(x):
            return len(x)
        def frame_of_caller():
            return sys._getframe(0)
        self.assertEqual(f("abc"), 3)
        # sys._getframe() is a builtin, so the calling frame stays.
        self.assertIs(frame_of_caller().f_code, frame_of_caller.__code__)

    def test_class_constructor(self):
        class K:
            def __init__(self, value):
                self.value = value
                self.caller = caller_name()
        def make(value):
            return K(value)
        k = make(42)
        self.assertEqual(k.value, 42)
        self.assertEqual(k.caller, "make")

    def test_callable_object(self):
        class Doubler:
            def __call__(self, value):
                return value * 2, caller_name()
        def apply(value):
            return Doubler()(value)
        self.assertEqual(apply(21), (42, "apply"))

    def test_generator_function(self):
        def gen(n):
            yield from range(n)
        def make(n):
            return gen(n)
        self.assertEqual(list(make(3)), [0, 1, 2])

    def test_try_finally_order(self):
        log = []
        def target(x):
            log.append(("target", caller_name()))
            return x
        def f(x):
            try:
                return target(x)
            finally:
                log.append("finally")
        self.assertEqual(f(1), 1)
        self.assertEqual(log, [("target", "f"), "finally"])

    def test_with_order(self):
        log = []
        class CM:
            def __enter__(self):
                log.append("enter")
            def __exit__(self, *exc):
                log.append("exit")
        def target(x):
            log.append(("target", caller_name()))
            return x
        def f(x):
            with CM():
                return target(x)
        self.assertEqual(f(2), 2)
        self.assertEqual(log, ["enter", ("target", "f"), "exit"])

    def test_generator(self):
        def target(x):
            return x, caller_name()
        def gen(x):
            yield "first"
            return target(x)
        g = gen(5)
        self.assertEqual(next(g), "first")
        with self.assertRaises(StopIteration) as cm:
            next(g)
        self.assertEqual(cm.exception.value, (5, "gen"))

    @support.no_tracing
    def test_non_tail_recursion(self):
        def f(n):
            if n == 0:
                return 0
            return 1 + f(n - 1)
        with self.assertRaises(RecursionError):
            f(1_000_000)

    def test_tracing_disables_elimination(self):
        old = sys.gettrace()
        sys.settrace(lambda *args: None)
        try:
            self.assertEqual(frames_at_bottom(20), 21)
        finally:
            sys.settrace(old)

    def test_profiling_disables_elimination(self):
        events = []
        def profiler(frame, event, arg):
            if event in ("call", "return", "c_call"):
                events.append((event, frame.f_code.co_name))
        def g():
            return len("x")
        def f():
            return g()
        old = sys.getprofile()
        sys.setprofile(profiler)
        try:
            f()
        finally:
            sys.setprofile(old)
        self.assertIn(("call", "f"), events)
        self.assertIn(("call", "g"), events)
        self.assertIn(("c_call", "g"), events)
        self.assertIn(("return", "f"), events)

    def test_sys_monitoring_disables_elimination(self):
        mon = sys.monitoring
        tool = mon.DEBUGGER_ID
        mon.use_tool_id(tool, "test_molt_tce")
        try:
            mon.set_events(tool, mon.events.PY_START)
            self.assertEqual(frames_at_bottom(20), 21)
        finally:
            mon.set_events(tool, 0)
            mon.free_tool_id(tool)


class OptOutTests(unittest.TestCase):

    code = (
        "import sys\n"
        "def count(n):\n"
        "    if n == 0:\n"
        "        return 'done'\n"
        "    return count(n - 1)\n"
        "try:\n"
        "    print(count(100_000))\n"
        "except RecursionError:\n"
        "    print('RecursionError')\n"
    )

    def run_code(self, *args, **env):
        res = script_helper.assert_python_ok(*args, "-c", self.code, **env)
        return res.out.decode().strip()

    def test_x_notce(self):
        self.assertEqual(self.run_code("-X", "notce"), "RecursionError")

    def test_env_var(self):
        self.assertEqual(self.run_code(PYTHONNOTCE="1"), "RecursionError")

    @requires_tce
    def test_ignore_environment(self):
        self.assertEqual(self.run_code("-E", PYTHONNOTCE="1"), "done")

    @requires_tce
    def test_default(self):
        self.assertEqual(self.run_code(PYTHONNOTCE=""), "done")


if __name__ == "__main__":
    unittest.main()
