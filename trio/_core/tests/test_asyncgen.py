import sys
import weakref
import pytest
import contextlib
from math import inf
from functools import partial

from ... import _core
from .tutil import gc_collect_harder, buggy_pypy_asyncgens, restore_unraisablehook


@pytest.mark.skipif(sys.version_info < (3, 10), reason="no aclosing() in stdlib<3.10")
def test_asyncgen_basics():
    collected = []

    async def example(cause):
        try:
            try:
                yield 42
            except GeneratorExit:
                pass
            await _core.checkpoint()
        except _core.Cancelled:
            assert "exhausted" not in cause
            task_name = _core.current_task().name
            assert cause in task_name or task_name == "<init>"
            assert _core.current_effective_deadline() == -inf
            with pytest.raises(_core.Cancelled):
                await _core.checkpoint()
            collected.append(cause)
        else:
            assert "async_main" in _core.current_task().name
            assert "exhausted" in cause
            assert _core.current_effective_deadline() == inf
            await _core.checkpoint()
            collected.append(cause)

    saved = []

    async def async_main():
        # GC'ed before exhausted
        with pytest.warns(
            ResourceWarning, match="Async generator.*collected before.*exhausted"
        ):
            assert 42 == await example("abandoned").asend(None)
            gc_collect_harder()
        await _core.wait_all_tasks_blocked()
        assert collected.pop() == "abandoned"

        # aclosing() ensures it's cleaned up at point of use
        async with contextlib.aclosing(example("exhausted 1")) as aiter:
            assert 42 == await aiter.asend(None)
        assert collected.pop() == "exhausted 1"

        # Also fine if you exhaust it at point of use
        async for val in example("exhausted 2"):
            assert val == 42
        assert collected.pop() == "exhausted 2"

        gc_collect_harder()

        # No problems saving the geniter when using either of these patterns
        async with contextlib.aclosing(example("exhausted 3")) as aiter:
            saved.append(aiter)
            assert 42 == await aiter.asend(None)
        assert collected.pop() == "exhausted 3"

        # Also fine if you exhaust it at point of use
        saved.append(example("exhausted 4"))
        async for val in saved[-1]:
            assert val == 42
        assert collected.pop() == "exhausted 4"

        # Leave one referenced-but-unexhausted and make sure it gets cleaned up
        if buggy_pypy_asyncgens:
            collected.append("outlived run")
        else:
            saved.append(example("outlived run"))
            assert 42 == await saved[-1].asend(None)
            assert collected == []

    _core.run(async_main)
    assert collected.pop() == "outlived run"
    for agen in saved:
        assert agen.ag_frame is None  # all should now be exhausted


async def test_asyncgen_throws_during_finalization(caplog):
    record = []

    async def agen():
        try:
            yield 1
        finally:
            await _core.cancel_shielded_checkpoint()
            record.append("crashing")
            raise ValueError("oops")

    with restore_unraisablehook():
        await agen().asend(None)
        gc_collect_harder()
    await _core.wait_all_tasks_blocked()
    assert record == ["crashing"]
    exc_type, exc_value, exc_traceback = caplog.records[0].exc_info
    assert exc_type is ValueError
    assert str(exc_value) == "oops"
    assert "during finalization of async generator" in caplog.records[0].message


@pytest.mark.skipif(buggy_pypy_asyncgens, reason="pypy 7.2.0 is buggy")
def test_firstiter_after_closing():
    saved = []
    record = []

    async def funky_agen():
        try:
            yield 1
        except GeneratorExit:
            record.append("cleanup 1")
            raise
        try:
            yield 2
        finally:
            record.append("cleanup 2")
            await funky_agen().asend(None)

    async def async_main():
        aiter = funky_agen()
        saved.append(aiter)
        assert 1 == await aiter.asend(None)
        assert 2 == await aiter.asend(None)

    _core.run(async_main)
    assert record == ["cleanup 2", "cleanup 1"]


@pytest.mark.skipif(buggy_pypy_asyncgens, reason="pypy 7.2.0 is buggy")
def test_interdependent_asyncgen_cleanup_order():
    saved = []
    record = []

    async def innermost():
        try:
            yield 1
        finally:
            await _core.cancel_shielded_checkpoint()
            record.append("innermost")

    async def agen(label, inner):
        try:
            yield await inner.asend(None)
        finally:
            # Either `inner` has already been cleaned up, or
            # we're about to exhaust it. Either way, we wind
            # up with `record` containing the labels in
            # innermost-to-outermost order.
            with pytest.raises(StopAsyncIteration):
                await inner.asend(None)
            record.append(label)

    async def async_main():
        # This makes a chain of 101 interdependent asyncgens:
        # agen(99)'s cleanup will iterate agen(98)'s will iterate
        # ... agen(0)'s will iterate innermost()'s
        ag_chain = innermost()
        for idx in range(100):
            ag_chain = agen(idx, ag_chain)
        saved.append(ag_chain)
        assert 1 == await ag_chain.asend(None)
        assert record == []

    _core.run(async_main)
    assert record == ["innermost"] + list(range(100))


@restore_unraisablehook()
def test_last_minute_gc_edge_case():
    saved = []
    record = []
    needs_retry = True

    async def agen():
        try:
            yield 1
        finally:
            record.append("cleaned up")

    def collect_at_opportune_moment(token):
        runner = _core._run.GLOBAL_RUN_CONTEXT.runner
        if runner.system_nursery._closed and isinstance(
            runner.asyncgens.alive, weakref.WeakSet
        ):
            saved.clear()
            record.append("final collection")
            gc_collect_harder()
            record.append("done")
        else:
            try:
                token.run_sync_soon(collect_at_opportune_moment, token)
            except _core.RunFinishedError:  # pragma: no cover
                nonlocal needs_retry
                needs_retry = True

    async def async_main():
        token = _core.current_trio_token()
        token.run_sync_soon(collect_at_opportune_moment, token)
        saved.append(agen())
        await saved[-1].asend(None)

    # Actually running into the edge case requires that the run_sync_soon task
    # execute in between the system nursery's closure and the strong-ification
    # of runner.asyncgens. There's about a 25% chance that it doesn't
    # (if the run_sync_soon task runs before init on one tick and after init
    # on the next tick); if we try enough times, we can make the chance of
    # failure as small as we want.
    for attempt in range(50):
        needs_retry = False
        del record[:]
        del saved[:]
        _core.run(async_main)
        if needs_retry:  # pragma: no cover
            if not buggy_pypy_asyncgens:
                assert record == ["cleaned up"]
        else:
            assert record == ["final collection", "done", "cleaned up"]
            break
    else:  # pragma: no cover
        pytest.fail(
            "Didn't manage to hit the trailing_finalizer_asyncgens case "
            f"despite trying {attempt} times"
        )


async def step_outside_async_context(aiter):
    # abort_fns run outside of task context, at least if they're
    # triggered by a deadline expiry rather than a direct
    # cancellation.  Thus, an asyncgen first iterated inside one
    # will appear non-Trio, and since no other hooks were installed,
    # will use the last-ditch fallback handling (that tries to mimic
    # CPython's behavior with no hooks).
    #
    # NB: the strangeness with aiter being an attribute of abort_fn is
    # to make it as easy as possible to ensure we don't hang onto a
    # reference to aiter inside the guts of the run loop.
    def abort_fn(_):
        with pytest.raises(StopIteration, match="42"):
            abort_fn.aiter.asend(None).send(None)
        del abort_fn.aiter
        return _core.Abort.SUCCEEDED

    abort_fn.aiter = aiter

    async with _core.open_nursery() as nursery:
        nursery.start_soon(_core.wait_task_rescheduled, abort_fn)
        await _core.wait_all_tasks_blocked()
        nursery.cancel_scope.deadline = _core.current_time()


@pytest.mark.skipif(buggy_pypy_asyncgens, reason="pypy 7.2.0 is buggy")
async def test_fallback_when_no_hook_claims_it(capsys):
    async def well_behaved():
        yield 42

    async def yields_after_yield():
        with pytest.raises(GeneratorExit):
            yield 42
        yield 100

    async def awaits_after_yield():
        with pytest.raises(GeneratorExit):
            yield 42
        await _core.cancel_shielded_checkpoint()

    with restore_unraisablehook():
        await step_outside_async_context(well_behaved())
        gc_collect_harder()
        assert capsys.readouterr().err == ""

        await step_outside_async_context(yields_after_yield())
        gc_collect_harder()
        assert "ignored GeneratorExit" in capsys.readouterr().err

        await step_outside_async_context(awaits_after_yield())
        gc_collect_harder()
        assert "awaited something during finalization" in capsys.readouterr().err


@pytest.mark.skipif(buggy_pypy_asyncgens, reason="pypy 7.2.0 is buggy")
def test_delegation_to_existing_hooks():
    record = []

    def my_firstiter(agen):
        record.append("firstiter " + agen.ag_frame.f_locals["arg"])

    def my_finalizer(agen):
        record.append("finalizer " + agen.ag_frame.f_locals["arg"])

    async def example(arg):
        try:
            yield 42
        finally:
            with pytest.raises(_core.Cancelled):
                await _core.checkpoint()
            record.append("trio collected " + arg)

    async def async_main():
        await step_outside_async_context(example("theirs"))
        assert 42 == await example("ours").asend(None)
        gc_collect_harder()
        assert record == ["firstiter theirs", "finalizer theirs"]
        record[:] = []
        await _core.wait_all_tasks_blocked()
        assert record == ["trio collected ours"]

    with restore_unraisablehook():
        old_hooks = sys.get_asyncgen_hooks()
        sys.set_asyncgen_hooks(my_firstiter, my_finalizer)
        try:
            _core.run(async_main)
        finally:
            assert sys.get_asyncgen_hooks() == (my_firstiter, my_finalizer)
            sys.set_asyncgen_hooks(*old_hooks)
