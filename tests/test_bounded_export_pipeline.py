from __future__ import annotations

import threading
import unittest
import weakref

from app.renderer.bounded_pipeline import (
    BoundedExportPipeline,
    ExportPipelineCancelledError,
    ExportPipelineClosedError,
    ExportPipelineConsumerError,
)


class BoundedExportPipelineTests(unittest.TestCase):
    def test_consumed_item_is_released_before_waiting_for_more_input(self) -> None:
        class Payload:
            pass

        released = threading.Event()
        pipeline = BoundedExportPipeline(lambda _value: None, capacity=1)
        pipeline.start()
        payload = Payload()
        weakref.finalize(payload, released.set)
        pipeline.submit(payload)
        del payload
        try:
            self.assertTrue(released.wait(1.0))
        finally:
            pipeline.finish(timeout_seconds=2.0)

    def test_order_is_preserved_and_pending_items_never_exceed_capacity(self) -> None:
        consumed: list[int] = []
        first_started = threading.Event()
        release_first = threading.Event()

        def consume(value: int) -> None:
            if value == 0:
                first_started.set()
                self.assertTrue(release_first.wait(2.0))
            consumed.append(value)

        pipeline = BoundedExportPipeline(consume, capacity=2)
        pipeline.start()
        pipeline.submit(0)
        self.assertTrue(first_started.wait(1.0))
        pipeline.submit(1)
        pipeline.submit(2)

        self.assertEqual(pipeline.pending_count, 2)
        self.assertEqual(pipeline.peak_buffered_items, 2)
        self.assertLessEqual(pipeline.peak_buffered_items, pipeline.capacity)

        release_first.set()
        pipeline.finish(timeout_seconds=2.0)
        self.assertEqual(consumed, [0, 1, 2])

    def test_consumer_failure_is_propagated_to_finish(self) -> None:
        failure = ValueError("simulated FFmpeg write failure")

        def consume(_value: int) -> None:
            raise failure

        pipeline = BoundedExportPipeline(consume, capacity=1)
        pipeline.start()
        pipeline.submit(1)

        with self.assertRaises(ExportPipelineConsumerError) as raised:
            pipeline.finish(timeout_seconds=2.0)
        self.assertIs(raised.exception.cause, failure)

    def test_cancel_wakes_a_blocked_producer_and_calls_cleanup_once(self) -> None:
        consumer_started = threading.Event()
        release_consumer = threading.Event()
        producer_started = threading.Event()
        producer_finished = threading.Event()
        producer_errors: list[BaseException] = []
        cancel_calls = 0

        def consume(_value: int) -> None:
            consumer_started.set()
            release_consumer.wait(2.0)

        def cancel_consumer() -> None:
            nonlocal cancel_calls
            cancel_calls += 1
            release_consumer.set()

        pipeline = BoundedExportPipeline(
            consume,
            capacity=1,
            on_cancel=cancel_consumer,
            poll_interval_seconds=0.01,
        )
        pipeline.start()
        pipeline.submit(0)
        self.assertTrue(consumer_started.wait(1.0))
        pipeline.submit(1)

        def submit_blocked_item() -> None:
            producer_started.set()
            try:
                pipeline.submit(2)
            except BaseException as error:
                producer_errors.append(error)
            finally:
                producer_finished.set()

        producer = threading.Thread(target=submit_blocked_item, daemon=True)
        producer.start()
        self.assertTrue(producer_started.wait(1.0))
        self.assertFalse(producer_finished.wait(0.05))

        pipeline.cancel()
        pipeline.cancel()
        self.assertTrue(producer_finished.wait(1.0))
        producer.join(1.0)
        self.assertEqual(cancel_calls, 1)
        self.assertEqual(len(producer_errors), 1)
        self.assertIsInstance(producer_errors[0], ExportPipelineCancelledError)
        with self.assertRaises(ExportPipelineCancelledError):
            pipeline.finish(timeout_seconds=2.0)

    def test_submit_after_successful_finish_is_rejected(self) -> None:
        pipeline = BoundedExportPipeline(lambda _value: None, capacity=1)
        pipeline.start()
        pipeline.submit(1)
        pipeline.finish(timeout_seconds=2.0)

        with self.assertRaises(ExportPipelineClosedError):
            pipeline.submit(2)

    def test_finish_pumps_wait_callback_while_consumer_drains(self) -> None:
        consumer_started = threading.Event()
        release_consumer = threading.Event()
        callback_calls = 0

        def consume(_value: int) -> None:
            consumer_started.set()
            release_consumer.wait(2.0)

        def pump() -> None:
            nonlocal callback_calls
            callback_calls += 1

        pipeline = BoundedExportPipeline(
            consume, capacity=1, poll_interval_seconds=0.01,
        )
        pipeline.start()
        pipeline.submit(1)
        self.assertTrue(consumer_started.wait(1.0))
        release_timer = threading.Timer(0.08, release_consumer.set)
        release_timer.start()
        try:
            pipeline.finish(timeout_seconds=1.0, wait_callback=pump)
        finally:
            release_timer.cancel()
        self.assertGreater(callback_calls, 0)

    def test_finish_pumps_callback_while_end_marker_waits_for_queue_space(self) -> None:
        consumer_started = threading.Event()
        release_consumer = threading.Event()
        callback_seen = threading.Event()

        def consume(_value: int) -> None:
            consumer_started.set()
            release_consumer.wait(2.0)

        pipeline = BoundedExportPipeline(
            consume, capacity=1, poll_interval_seconds=0.01,
        )
        pipeline.start()
        pipeline.submit(1)
        self.assertTrue(consumer_started.wait(1.0))
        # This second item occupies the only queue slot while the first item is
        # still being consumed. finish() must remain responsive even before it
        # can enqueue the end-of-stream marker.
        pipeline.submit(2)

        release_timer = threading.Timer(0.08, release_consumer.set)
        release_timer.start()
        try:
            pipeline.finish(
                timeout_seconds=1.0, wait_callback=callback_seen.set,
            )
        finally:
            release_timer.cancel()
        self.assertTrue(callback_seen.is_set())

    def test_capacity_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            BoundedExportPipeline(lambda _value: None, capacity=0)


if __name__ == "__main__":
    unittest.main()
