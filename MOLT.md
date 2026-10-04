# Molt: CPython with tail call elimination

Molt is a fork of CPython 3.16.0a0 (`main` at `9d22a5334b`) in which a Python
function that ends in `return f(...)` hands its frame over to `f` instead of
stacking a new frame on top of its own. Tail-recursive code runs in constant
stack space and is not limited by `sys.getrecursionlimit()`:

```python
def count(n, acc=0):
    if n == 0:
        return acc
    return count(n - 1, acc + 1)

count(10_000_000)  # 10000000; stock CPython raises RecursionError
```

Everything else behaves like CPython, except that eliminated frames no longer
appear on the stack. See [Known differences](#known-differences-from-cpython).

## Building and testing

Build exactly like CPython, for example `./configure --with-pydebug && make -j`
(the interpreter is `./python.exe` on macOS, `./python` elsewhere).

- `./python -m test test_molt_tce` runs molt's own tests.
- `make test` runs the full suite; see [Test suite](#test-suite) for the
  expected differences.
- `./python -m dis script.py` shows which calls compile to tail calls.
- After changing `Python/bytecodes.c`, run `make regen-all`.

## Installing next to your normal Python

Molt installs into its own prefix and is run as `molt`, so `python3` keeps
running whatever Python you normally use. `make altinstall` only creates
versioned names such as `python3.16`, never `python3` or `python`. An
optimized build is much faster than the `--with-pydebug` development build.
Build it from a clean checkout, because CPython can't build out of tree
from a source directory that has been configured in place:

```sh
git worktree add --detach builddir/src molt
mkdir -p builddir/release && cd builddir/release
../src/configure --prefix="$HOME/.local/molt"
make -j && make altinstall
ln -s "$HOME/.local/molt/bin/python3.16" "$HOME/.local/bin/molt"
```

`~/.local/bin` must be on your `PATH`. After that:

```sh
molt script.py              # run a script with molt
molt -m venv .venv          # a virtual environment that uses molt
molt -m pip install pkg     # installs into ~/.local/molt only
```

To update after new commits, run
`git -C builddir/src checkout --detach molt`, then repeat
`make -j && make altinstall` in `builddir/release`. To uninstall, delete
`~/.local/molt` and `~/.local/bin/molt`.

## Seeing eliminated frames

Every frame counts how many frames tail calls replaced in its place, and
tracebacks show the count where frames are missing:

```text
Traceback (most recent call last):
  File "demo.py", line 6, in <module>
    count(5)
    ~~~~~^^^
  [5 tail calls eliminated]
  File "demo.py", line 3, in count
    raise ValueError("bottom")
ValueError: bottom
```

The same line appears in `traceback.format_stack()` and other `traceback`
module output, in the C traceback printer, and in faulthandler dumps. There
it follows the frame, because faulthandler lists the most recent call first.
`frame.f_tail_calls` gives the count for a frame, and
`sys._tail_calls_eliminated()` returns how many frames tail calls have
replaced in the current thread.

## Rules for tail position

A call is compiled as a tail call when all of these hold:

- It is the entire value of a `return` statement, as in `return f(x)` (but not
  `return f(x) + 1` or `return (f(x), y)`), or the entire body of a `lambda`.
  In `return f(g(x))` only `f` is a tail call.
- The function is an ordinary function, not a generator, coroutine or async
  generator.
- The `return` is not inside any part of a `try` statement (including its
  `except`, `else` and `finally` blocks), a `with` or an `async with`, because
  cleanup code has to run after the call. Enclosing `for` and `while` loops are
  fine.

Module and class bodies cannot contain `return`, so they never contain tail
calls. Branches of conditional expressions (`return a if c else f(x)`) and
operands of `and`/`or` are not tail positions.

The compiler emits `TAIL_CALL`, `TAIL_CALL_KW` or `TAIL_CALL_EX` in place of
`CALL`, `CALL_KW` or `CALL_FUNCTION_EX`, followed by the usual `RETURN_VALUE`.

## When a tail call is eliminated

At run time a tail call replaces the current frame only if all of these hold:

- The callee is a plain Python function, or a bound method of one. Builtins,
  classes, objects with `__call__`, and generator or coroutine functions are
  called normally.
- Elimination has not been turned off with `-X notce` or `PYTHONNOTCE`.
- No `sys.monitoring` events are active for the calling code. This includes
  `sys.settrace()` and `sys.setprofile()`, so debuggers (pdb), profilers
  (cProfile) and coverage tools see every frame.
- No PEP 523 frame evaluation hook is installed (`-X perf` installs one).
- The build is not free-threaded.

Otherwise the tail call behaves exactly like `CALL`, and the `RETURN_VALUE`
after it returns the result.

## How elimination works

1. Any argument that is a borrowed reference to one of the current frame's
   locals is turned into an owned reference, because that frame is about to go
   away.
2. The callee's frame is built directly on top of the current frame, using the
   normal argument-binding code. If binding fails, for example because of a
   missing argument, the `TypeError` is raised in the calling frame, as in
   CPython.
3. The current frame is cleared exactly as `RETURN_VALUE` would clear it: it
   is unlinked, its locals are released, and a frame object that outlives it
   keeps a copy.
4. The callee's frame is moved down into the freed space, linked to the
   original caller, and starts running. When it returns, it returns straight
   to that caller.

The recursion counter goes down for the old frame and up for the new one, so
the depth stays constant. The main pieces are:

| File | What it contains |
| --- | --- |
| `Python/codegen.c`, `Python/compile.c` | Tail-position detection |
| `Python/bytecodes.c` | The three opcodes, their `INSTRUMENTED_` variants, and the elimination branch in `_DO_CALL`, `_DO_CALL_KW` and `_DO_CALL_FUNCTION_EX` |
| `Python/ceval.c`, `Python/ceval_macros.h` | `_PyEval_CanEliminateTailCall()`, `_PyEval_FrameClearAndReplace()`, `DISPATCH_TAIL_CALL` |
| `Python/instrumentation.c` | `sys.monitoring` tables for the new opcodes |
| `Python/pylifecycle.c` | `-X notce` and `PYTHONNOTCE` |
| `Include/internal/pycore_interpframe_structs.h`, `Objects/frameobject.c`, `Python/sysmodule.c` | The per-frame and per-thread counts, `frame.f_tail_calls`, `sys._tail_calls_eliminated()`, and `sys._getframemodulename()` counting eliminated frames |
| `Lib/traceback.py`, `Python/traceback.c` | `[N tail calls eliminated]` in tracebacks and faulthandler dumps |
| `Lib/test/test_molt_tce.py` | Tests |

## Turning it off

Run with `-X notce`, or set `PYTHONNOTCE` to any non-empty value, to turn
elimination off for the whole process, including subinterpreters.
`PYTHONNOTCE` is ignored under `-E` and `-I`, like other `PYTHON*` variables.
The compiled bytecode is the same either way, so `.pyc` files don't change.

Turning it off is also the quickest way to check whether a problem is caused
by tail call elimination.

## Known differences from CPython

- **Eliminated frames are gone.** They don't appear in tracebacks,
  `sys._getframe()`, `frame.f_back`, `inspect.stack()`,
  `traceback.extract_stack()` or faulthandler dumps. Tracebacks and dumps
  only show how many are missing.
- **Infinite tail recursion never stops.** `def f(): return f()` loops forever
  instead of raising `RecursionError`, like `while True: pass`. Ctrl-C still
  interrupts it.
- **Locals are released earlier.** The eliminated frame's local variables, and
  the iterators of enclosing `for` loops, are released when the tail call
  starts rather than after it returns. `__del__` methods, weakref callbacks and
  generator `finally` blocks for objects that only that frame referenced
  therefore run earlier.
- **Code that inspects its caller can find a different frame.** If the
  caller, or a frame in between, was eliminated, code that looks up the
  stack finds the next surviving frame instead:
  - `return namedtuple("P", "x")` or `return make_dataclass("D", ["x"])` in
    module `a`, called from module `b`, creates a class whose `__module__`
    is `b`, which breaks pickling. The idiom
    `def test_suite(): return doctest.DocTestSuite()` collects the wrong
    module's doctests. Assign the result to a variable before returning it
    to avoid this.
  - A warning with a fixed `stacklevel` that passes through a tail call
    points one frame too high. For example, warnings from `re.compile()`
    point at its caller's caller.
  - `sys._getframe(n)`, `frame.f_back`, `inspect.stack()`, logging's caller
    information and the `traceback` module only see frames that still
    exist. Because they agree with each other, code that computes a
    `stacklevel` by walking `f_back` keeps working.
- **`sys._getframemodulename()` counts eliminated frames.** Code calls it
  with a fixed depth to find the module that called it, so it treats each
  eliminated frame as one level. That way `Enum("Color", "RED GREEN")` still
  records the right module, and the enum can be pickled, even though
  `EnumType.__call__` ends in a tail call. As a result,
  `sys._getframemodulename(n)` can name a different module than
  `sys._getframe(n)`.
- **Debuggers can't recover frames.** Attaching a debugger mid-run (for
  example with `breakpoint()`) stops further elimination, but frames that
  were eliminated before it attached stay gone.
- **`return f(...)` is not specialized.** The adaptive interpreter does not
  rewrite tail calls into fast paths such as `CALL_PY_EXACT_ARGS` or
  `CALL_LEN`, so these calls are somewhat slower than in CPython. The
  experimental JIT can't compile them either, so each tail call leaves
  compiled code. On a debug JIT build, `count(10_000_000)` took 5.5 s with
  the JIT on and 3.6 s with it off.
- **Bytecode differs.** There are three new opcodes plus their instrumented
  variants, several existing opcodes are renumbered, and the magic number is
  3749. Molt and stock CPython 3.16 share the `cpython-316` cache tag, so
  switching between them recompiles `.pyc` files.
- **Free-threaded builds don't eliminate tail calls.** The callee's frame would
  hold deferred references that the garbage collector can't see while the old
  frame is being cleared.

Molt has been built and tested on macOS (arm64) in these configurations:

| Build | Result |
| --- | --- |
| Default debug (`--with-pydebug`) | Full test suite; see [Test suite](#test-suite) |
| Optimized (no `--with-pydebug`), installed as `molt` | `test_molt_tce` passes |
| `--with-tail-call-interp` | `test_molt_tce` passes; same frame and traceback failures as the default build |
| `--enable-experimental-jit` (LLVM 21) | `test_molt_tce` passes; same failures as the default build, plus two `test_capi.test_opt` tests because tail calls aren't specialized |
| `--disable-gil` (free-threaded) | Elimination is off by design: the 27 tests that need it are skipped and the rest pass. The frame and traceback tests pass; `test_dis` fails as on the default build |

## Test suite

An unmodified build of the same commit passes `make test` with no failures.
Molt passes `test_molt_tce` and fails 15 other test files, all caused by the
differences above. No existing tests were changed.

Infinite tail recursion that the test expects to end in `RecursionError` runs
until the test times out:

- `test_exceptions`: `ExceptionTests.testInfiniteRecursion`
- `test_opcache`: `TestCallCache.test_recursion_check_for_general_calls`
- `test_threading`: `ThreadingExceptionTests.test_recursion_limit`

These loops can outlive the test run: the `test_threading` child process,
and sometimes the worker processes that re-run the timed-out tests, keep
spinning. After a full run, check with `pgrep -fl python` and kill any
leftovers. The traceback that faulthandler prints on a timeout may end in
`line ???` and `<invalid frame>`, because it reads the looping thread's frames
while they are being replaced.

The test expects a frame that is now eliminated:

- `test_traceback` (13 tests): helpers that end in `return g()`,
  `return traceback.extract_stack()` or a tail-called lambda.
- `test_frame` (7 tests): the `make_frames()` helper's `outer()` ends in
  `return inner()`.
- `test_contextlib_async`: `TestAsyncExitStack.test_exit_exception_traceback`
- `test_coroutines`: `OriginTrackingTest.test_origin_tracking_warning`
- `test_tracemalloc`: `TestTracemallocEnabled.test_get_traces_intern_traceback`
- `test_remote_pdb`: `PdbConnectTestCase.test_connect_and_basic_commands`
  (the frame is eliminated before the debugger attaches).
- `test_doctest`: `test_DocTestSuite`, and `test_zipimport_support`:
  `ZipSupportTests.test_doctest_issue4197`. Both use
  `def test_suite(): return doctest.DocTestSuite()`, which then finds the
  wrong "calling module".
- `test_re`: `ReTests.test_set_operations` checks where a warning from
  `re.compile()` points (see above).
- `test_sys`: `SysModuleTest.test_getframemodulename` checks that
  `sys._getframemodulename(n)` names the module of `sys._getframe(n)`,
  which is no longer true when eliminated frames are in between.

The test checks for `CALL` or for call specializations:

- `test_compile`: `TestSpecifics.test_imported_load_method`
- `test_dis` (2 tests): `test_disassemble_recursive` compares against a
  disassembly that contains `CALL`.
- `test_opcache` (5 tests): `test_call_c_function_extra_flags`,
  `test_assign_init_code`, `test_push_init_frame_fails`,
  `test_specialize_call_function_ex_py` and
  `test_specialize_call_function_ex_py_fail`.

To keep a full run short, use `make test TESTTIMEOUT=300`.

## Rebasing onto newer CPython

- Conflicts are most likely in `_DO_CALL`, `_DO_CALL_KW` and
  `_DO_CALL_FUNCTION_EX` in `Python/bytecodes.c`. After resolving them, run
  `make regen-all` and rebuild.
- Keep molt's magic number out of upstream's sequence by using the last number
  of the new version's range (3.16 uses 3700-3749).
- Change the magic number whenever molt changes what the compiler emits.
  Otherwise existing `.pyc` files stay valid and keep running the old
  bytecode. Clearing `__pycache__` directories has the same effect in a
  development tree.
- When upstream changes call opcodes, also update the hand-written tables in
  `Python/instrumentation.c` and `_cache_format` in `Lib/opcode.py`.
