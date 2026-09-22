import unittest
from utils.popcorn_intervention import PublishIntervention, ManualPublishCompleted, ManualPublishUncertain


class PublishInterventionTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_success_never_repeats_failed_operation(self):
        events, calls = [], []
        async def operation():
            calls.append(True)
            raise TimeoutError('button missing')
        async def probe():
            return 'published'
        async def control():
            return None
        interaction = PublishIntervention(headed=True, emit=events.append,
            probe=probe, control=control, session_id='session', now=lambda: 100)
        with self.assertRaises(ManualPublishCompleted):
            await interaction.step(operation)
        self.assertEqual(len(calls), 1)
        self.assertEqual(events[0], {'type': 'checkpoint', 'checkpoint': 'manual_control'})
        self.assertEqual(events[-1], {'type': 'result', 'status': 'published'})

    async def test_continue_rechecks_success_before_retry(self):
        calls, probes = [], []
        async def operation():
            calls.append(True)
            if len(calls) == 1:
                raise TimeoutError('button missing')
            return 'ok'
        async def probe():
            probes.append(True)
            return None
        async def control():
            return {'sessionId': 'session', 'action': 'continue'}
        interaction = PublishIntervention(headed=True, emit=lambda _: None,
            probe=probe, control=control, session_id='session', now=lambda: 100)
        self.assertEqual(await interaction.step(operation), 'ok')
        self.assertGreaterEqual(len(probes), 2)
        self.assertEqual(len(calls), 2)

    async def test_cancel_is_uncertain_after_browser_exposure(self):
        async def operation():
            raise TimeoutError('button missing')
        async def probe():
            return None
        async def control():
            return {'sessionId': 'session', 'action': 'cancel'}
        interaction = PublishIntervention(headed=True, emit=lambda _: None,
            probe=probe, control=control, session_id='session', now=lambda: 100)
        with self.assertRaises(ManualPublishUncertain):
            await interaction.step(operation)

    async def test_headless_emits_attention_without_waiting_for_user(self):
        events = []
        async def operation():
            raise TimeoutError('button missing')
        async def never():
            self.fail('headless must not wait for a user')
        interaction = PublishIntervention(headed=False, emit=events.append,
            probe=never, control=never, session_id='session')
        with self.assertRaises(TimeoutError):
            await interaction.step(operation)
        self.assertEqual(events[0]['kind'], 'page_blocked')


class PublishInterventionBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_uses_one_total_deadline_across_continuations(self):
        clock = [0]
        async def operation():
            raise TimeoutError('button missing')
        async def probe():
            return None
        async def control():
            return None
        async def sleep(seconds):
            clock[0] += 300
        interaction = PublishIntervention(headed=True, emit=lambda _: None,
            probe=probe, control=control, session_id='session', now=lambda: clock[0], sleep=sleep)
        with self.assertRaises(ManualPublishUncertain):
            await interaction.step(operation)
        self.assertEqual(clock[0], 600)

    async def test_network_error_does_not_open_manual_control(self):
        events = []
        async def operation():
            raise TimeoutError('net::ERR_CONNECTION_RESET')
        async def never():
            self.fail('network error must retain existing retry policy')
        interaction = PublishIntervention(headed=True, emit=events.append,
            probe=never, control=never, session_id='session')
        with self.assertRaises(TimeoutError):
            await interaction.step(operation)
        self.assertEqual(events, [])

    async def test_each_wait_uses_fresh_control_token(self):
        events = []
        async def probe():
            return None
        async def control():
            waiting = next(event for event in reversed(events) if event.get('state') == 'waiting')
            return {'sessionId': waiting['sessionId'], 'action': 'continue'}
        interaction = PublishIntervention(headed=True, emit=events.append,
            probe=probe, control=control, session_id='session')
        await interaction.wait_for_user('first')
        await interaction.wait_for_user('second')
        ids = [event['sessionId'] for event in events if event.get('state') == 'waiting']
        self.assertNotEqual(ids[0], ids[1])


if __name__ == '__main__':
    unittest.main()
