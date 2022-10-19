import os
import sys
from unittest.mock import MagicMock, patch

import pytest

from gradio.queue import Event, Queue
from gradio.utils import Request

os.environ["GRADIO_ANALYTICS_ENABLED"] = "False"


class AsyncMock(MagicMock):
    async def __call__(self, *args, **kwargs):
        return super(AsyncMock, self).__call__(*args, **kwargs)


@pytest.fixture()
def queue() -> Queue:
    queue_object = Queue(
        live_updates=True,
        concurrency_count=1,
        data_gathering_start=1,
        update_intervals=1,
        max_size=None,
    )
    yield queue_object
    queue_object.close()


@pytest.fixture()
def mock_event() -> Event:
    websocket = MagicMock()
    event = Event(websocket=websocket)
    yield event


class TestQueueMethods:
    @pytest.mark.asyncio
    async def test_start(self, queue: Queue):
        await queue.start()
        assert queue.stopped is False
        assert queue.get_active_worker_count() == 0

    @pytest.mark.asyncio
    async def test_dont_gather_data_while_broadcasting(self, queue: Queue):
        queue.broadcast_live_estimations = AsyncMock()
        queue.gather_data_for_first_ranks = AsyncMock()
        await queue.broadcast_live_estimations()

        # Should not gather data while broadcasting estimations
        # Have seen weird race conditions come up in very viral
        # spaces
        queue.gather_data_for_first_ranks.assert_not_called()

    @pytest.mark.asyncio
    async def test_stop_resume(self, queue: Queue):
        await queue.start()
        queue.close()
        assert queue.stopped
        queue.resume()
        assert queue.stopped is False

    @pytest.mark.asyncio
    async def test_receive(self, queue: Queue, mock_event: Event):
        await queue.get_message(mock_event)
        assert mock_event.websocket.receive_json.called

    @pytest.mark.asyncio
    async def test_send(self, queue: Queue, mock_event: Event):
        await queue.send_message(mock_event, {})
        assert mock_event.websocket.send_json.called

    @pytest.mark.asyncio
    async def test_add_to_queue(self, queue: Queue, mock_event: Event):
        queue.push(mock_event)
        assert len(queue.event_queue) == 1

    @pytest.mark.asyncio
    async def test_add_to_queue_with_max_size(self, queue: Queue, mock_event: Event):
        queue.max_size = 1
        queue.push(mock_event)
        assert len(queue.event_queue) == 1
        queue.push(mock_event)
        assert len(queue.event_queue) == 1

    @pytest.mark.asyncio
    async def test_clean_event(self, queue: Queue, mock_event: Event):
        queue.push(mock_event)
        await queue.clean_event(mock_event)
        assert len(queue.event_queue) == 0

    @pytest.mark.asyncio
    async def test_gather_event_data(self, queue: Queue, mock_event: Event):
        queue.send_message = AsyncMock()
        queue.get_message = AsyncMock()
        queue.send_message.return_value = True
        queue.get_message.return_value = {"data": ["test"], "fn": 0}

        assert await queue.gather_event_data(mock_event)
        assert queue.send_message.called
        assert mock_event.data == {"data": ["test"], "fn": 0}

        queue.send_message.called = False
        assert await queue.gather_event_data(mock_event)
        assert not (queue.send_message.called)

    @pytest.mark.asyncio
    async def test_gather_data_for_first_ranks(self, queue: Queue, mock_event: Event):
        websocket = MagicMock()
        mock_event2 = Event(websocket=websocket)
        queue.send_message = AsyncMock()
        queue.get_message = AsyncMock()
        queue.send_message.return_value = True
        queue.get_message.return_value = {"data": ["test"], "fn": 0}

        queue.push(mock_event)
        queue.push(mock_event2)
        await queue.gather_data_for_first_ranks()
        assert mock_event.data is not None
        assert mock_event2.data is None


class TestQueueEstimation:
    def test_get_update_estimation(self, queue: Queue):
        queue.update_estimation(5)
        estimation = queue.get_estimation()
        assert estimation.avg_event_process_time == 5

        queue.update_estimation(15)
        estimation = queue.get_estimation()
        assert estimation.avg_event_process_time == 10  # (5 + 15) / 2

        queue.update_estimation(100)
        estimation = queue.get_estimation()
        assert estimation.avg_event_process_time == 40  # (5 + 15 + 100) / 3

    @pytest.mark.asyncio
    async def test_send_estimation(self, queue: Queue, mock_event: Event):
        queue.send_message = AsyncMock()
        queue.send_message.return_value = True
        estimation = queue.get_estimation()
        estimation = await queue.send_estimation(mock_event, estimation, 1)
        assert queue.send_message.called
        assert estimation.rank == 1

        queue.update_estimation(5)
        estimation = await queue.send_estimation(mock_event, estimation, 2)
        assert estimation.rank == 2
        assert estimation.rank_eta == 15

    @pytest.mark.asyncio
    async def queue_sets_concurrency_count(self):
        queue_object = Queue(
            live_updates=True,
            concurrency_count=5,
            data_gathering_start=1,
            update_intervals=1,
            max_size=None,
        )
        assert len(queue_object.active_jobs) == 5
        queue_object.close()


class TestQueueProcessEvents:
    @pytest.mark.skipif(
        sys.version_info < (3, 8),
        reason="Mocks of async context manager don't work for 3.7",
    )
    @pytest.mark.asyncio
    @patch("gradio.queue.Request", new_callable=AsyncMock)
    async def test_process_event(self, mock_request, queue: Queue, mock_event: Event):
        queue.gather_event_data = AsyncMock()
        queue.gather_event_data.return_value = True
        queue.send_message = AsyncMock()
        queue.send_message.return_value = True
        queue.call_prediction = AsyncMock()
        queue.call_prediction.return_value = MagicMock()
        queue.call_prediction.return_value.has_exception = False
        queue.call_prediction.return_value.json = {"is_generating": False}
        mock_event.disconnect = AsyncMock()
        queue.clean_event = AsyncMock()
        await queue.process_event(mock_event)

        queue.call_prediction.assert_called_once()
        mock_event.disconnect.assert_called_once()
        queue.clean_event.assert_called_once()
        mock_request.assert_called_with(
            method=Request.Method.POST,
            url=f"{queue.server_path}reset",
            json={
                "session_hash": mock_event.session_hash,
                "fn_index": mock_event.fn_index,
            },
        )

    @pytest.mark.asyncio
    async def test_process_event_handles_error_when_gathering_data(
        self, queue: Queue, mock_event: Event
    ):
        mock_event.websocket.send_json = AsyncMock()
        mock_event.websocket.send_json.side_effect = ValueError("Can't connect")
        queue.call_prediction = AsyncMock()
        mock_event.disconnect = AsyncMock()
        queue.clean_event = AsyncMock()
        mock_event.data = None
        await queue.process_event(mock_event)
        assert not queue.call_prediction.called
        mock_event.disconnect.assert_called_once()
        assert queue.clean_event.call_count >= 1

    @pytest.mark.asyncio
    async def test_process_event_handles_error_sending_process_start_msg(
        self, queue: Queue, mock_event: Event
    ):
        mock_event.websocket.send_json = AsyncMock()
        mock_event.websocket.send_json.side_effect = ["2", ValueError("Can't connect")]
        queue.call_prediction = AsyncMock()
        mock_event.disconnect = AsyncMock()
        queue.clean_event = AsyncMock()
        mock_event.data = None
        await queue.process_event(mock_event)
        assert not queue.call_prediction.called
        mock_event.disconnect.assert_called_once()
        assert queue.clean_event.call_count >= 1

    @pytest.mark.asyncio
    async def test_process_event_handles_exception_in_call_prediction_request(
        self, queue: Queue, mock_event: Event
    ):
        mock_event.disconnect = AsyncMock()
        queue.gather_event_data = AsyncMock(return_value=True)
        queue.clean_event = AsyncMock()
        queue.send_message = AsyncMock(return_value=True)
        queue.call_prediction = AsyncMock(
            return_value=MagicMock(has_exception=True, exception=ValueError("foo"))
        )
        await queue.process_event(mock_event)
        queue.call_prediction.assert_called_once()
        mock_event.disconnect.assert_called_once()
        assert queue.clean_event.call_count >= 1

    @pytest.mark.asyncio
    async def test_process_event_handles_error_sending_process_completed_msg(
        self, queue: Queue, mock_event: Event
    ):
        mock_event.websocket.send_json = AsyncMock()
        mock_event.websocket.send_json.side_effect = [
            "2",
            "3",
            ValueError("Can't connect"),
        ]
        queue.call_prediction = AsyncMock(
            return_value=MagicMock(has_exception=False, json=dict(is_generating=False))
        )
        mock_event.disconnect = AsyncMock()
        queue.clean_event = AsyncMock()
        mock_event.data = None
        await queue.process_event(mock_event)
        queue.call_prediction.assert_called_once()
        mock_event.disconnect.assert_called_once()
        assert queue.clean_event.call_count >= 1

    @pytest.mark.skipif(
        sys.version_info < (3, 8),
        reason="Mocks of async context manager don't work for 3.7",
    )
    @pytest.mark.asyncio
    @patch("gradio.queue.Request", new_callable=AsyncMock)
    async def test_process_event_handles_exception_during_disconnect(
        self, mock_request, queue: Queue, mock_event: Event
    ):
        mock_event.websocket.send_json = AsyncMock()
        queue.call_prediction = AsyncMock(
            return_value=MagicMock(has_exception=False, json=dict(is_generating=False))
        )
        # No exception should be raised during `process_event`
        mock_event.disconnect = AsyncMock(side_effect=ValueError("..."))
        queue.clean_event = AsyncMock()
        mock_event.data = None
        await queue.process_event(mock_event)
        mock_request.assert_called_with(
            method=Request.Method.POST,
            url=f"{queue.server_path}reset",
            json={
                "session_hash": mock_event.session_hash,
                "fn_index": mock_event.fn_index,
            },
        )
