from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from playwright.sync_api import Browser, sync_playwright, TimeoutError as PlaywrightTimeoutError

import main
import manage_schedule
import runtime


class ConfigTests(unittest.TestCase):
    def test_time_formats_and_validation(self):
        for text, expected in [('06', (6, None)), ('06:30', (6, 30)), ('18시30분', (18, 30)), ('1830', (18, 30))]:
            self.assertEqual(main._parse_dispatch_time_kw(text), expected)
        for text in ('24:00', '06:99', '06:30oops', '', 'abcd'):
            with self.assertRaises(main.ConfigError):
                main._parse_dispatch_time_kw(text)

    def test_redaction(self):
        with patch.dict(os.environ, {'BUS_USERNAME': 'private-user', 'BUS_PASSWORD': 'secret-password'}):
            text = main.redact('private-user secret-password https://host/page?moonToken=abc&x=2 '
                               'https://discord.com/api/webhooks/123/secret moonToken=abc')
        for secret in ('private-user', 'secret-password', 'abc', '/123/secret'):
            self.assertNotIn(secret, text)

    def test_scheduler_skips_missed_slots(self):
        self.assertEqual(manage_schedule.INTERVAL_SECONDS, 900)
        self.assertEqual(manage_schedule.next_due(100, 100, 900), 1000)
        self.assertEqual(manage_schedule.next_due(1000, 3000, 900), 3700)


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.output = patch('main.log')
        self.output.start()
        self.config = patch('main.load_config', return_value={})
        self.config.start()
        self.sender = patch('main.send_discord_webhook', return_value=True)
        self.send = self.sender.start()
        self.sleep_patch = patch('main.time.sleep')
        self.sleep = self.sleep_patch.start()
        self.env = patch.dict(os.environ, {'LH_BUSRO_SAVE_SEATS_JSON_ON_ALERT': 'false'})
        self.env.start()
        self.addCleanup(patch.stopall)

    def test_transient_recovery_no_error_notification(self):
        failure = main.AttemptFailure('login', PlaywrightTimeoutError('slow'))
        with patch('main.run_attempt', side_effect=[failure, main.Result('closed')]) as run:
            self.assertEqual(main.run_check(), 0)
        self.assertEqual(run.call_count, 2)
        self.sleep.assert_called_once_with(5)
        self.send.assert_not_called()

    def test_two_failures_notify_once(self):
        failure = main.AttemptFailure('query', main.SiteError('offline'))
        with patch('main.run_attempt', side_effect=failure) as run:
            self.assertEqual(main.run_check(), 1)
        self.assertEqual(run.call_count, 2)
        self.send.assert_called_once()

    def test_code_and_auth_errors_not_retried(self):
        for cause in (NameError('missing_function'), main.AuthenticationError('denied'), main.ConfigError('ambiguous')):
            with patch('main.run_attempt', side_effect=main.AttemptFailure('stage', cause)) as run:
                self.assertEqual(main.run_check(), 1)
                run.assert_called_once()

    def test_webhook_failure_does_not_repeat_lookup(self):
        self.send.return_value = False
        result = main.Result('checked', {'available': [17], 'unavailable': [], 'unknown': []})
        with patch('main.run_attempt', return_value=result) as run:
            self.assertEqual(main.run_check(), 1)
            run.assert_called_once()
        self.send.assert_called_once()

    def test_excluded_seats_no_notification(self):
        result = main.Result('checked', {'available': [25, 26], 'unavailable': [17], 'unknown': []})
        with patch('main.run_attempt', return_value=result):
            self.assertEqual(main.run_check(), 0)
        self.send.assert_not_called()

    def test_disabled_notification_never_posts(self):
        # Exercise the real sender, not the mock installed above.
        self.sender.stop()
        with patch.dict(os.environ, {'DISCORD_WEBHOOK_URL': 'https://example.test/webhook'}), patch('main.requests.post') as post:
            self.assertTrue(main.send_discord_webhook('test', notify=False))
            post.assert_not_called()


class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = sync_playwright().start()
        try:
            cls.browser = cls.engine.chromium.launch(headless=True)
        except Exception:
            cls.engine.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.engine.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.addCleanup(self.page.close)

    def test_popup_uses_visible_close_inside_popup(self):
        self.page.set_content('''<button style="display:none">닫기</button>
            <button>닫기</button><div id="mLayer_1">
            <button style="display:none">닫기</button>
            <button onclick="document.getElementById('mLayer_1').remove()">닫기</button></div>''')
        self.assertTrue(main.close_popup_if_exists(self.page))
        self.assertFalse(self.page.locator('#mLayer_1').count())

    def test_late_popup_blocks_click_then_recovers(self):
        self.page.set_content('''<button id="target" style="display:none" onclick="this.textContent='done'">조회</button>
        <script>setTimeout(() => {
            document.querySelector('#target').style.display='block';
            document.body.insertAdjacentHTML('beforeend', `<div id="mLayer_1" style="position:fixed;inset:0;background:white;z-index:10">
                <button onclick="this.parentElement.remove()">닫기</button></div>`);
        }, 100);</script>''')
        target = self.page.locator('#target')
        main.click_and_wait(self.page, target, lambda: target.inner_text() == 'done', 'clicked')
        self.assertEqual(target.inner_text(), 'done')

    def test_completed_click_not_repeated(self):
        self.page.set_content('<p>ready</p>')
        locator = Mock()
        locator.click.side_effect = PlaywrightTimeoutError('click action done; waiting for scheduled navigations')
        main.click_and_wait(self.page, locator, lambda: True, 'ready')
        locator.click.assert_called_once()

    def test_delayed_state_and_login_rejection(self):
        self.page.set_content('''<div id="ready" hidden>ready</div>
            <script>setTimeout(()=>document.querySelector("#ready").hidden=false,100)</script>''')
        main.wait_ready(self.page, self.page.locator('#ready').is_visible, 'ready', timeout_ms=1000)
        with self.assertRaises(main.AuthenticationError):
            main.wait_ready(self.page, lambda: False, 'login', allow_login=True, dialogs=['아이디 또는 비밀번호가 일치하지 않습니다'])

    def test_error_page(self):
        with self.assertRaises(main.SiteError):
            main.check_page(Mock(url='chrome-error://chromewebdata/'))
        with self.assertRaises(main.SiteError):
            main.check_page(Mock(url='https://lh.busro.net:456/syscon/error.html?moonToken=private'))
        with self.assertRaises(main.SiteError):
            main.check_page(Mock(url='https://lh.busro.net:456/rsvc/login.html'))

    def test_seat_parsing_disabled_and_hidden(self):
        self.page.set_content('''<table><tr>
            <td class="vwSeatTd17"><input type="checkbox"></td>
            <td class="vwSeatTd18"><input type="checkbox" disabled></td>
            <td class="vwSeatTd19"><img alt="예약불가"></td>
            <td class="vwSeatTd20" style="display:none"><input type="checkbox"></td>
            </tr></table>''')
        self.assertEqual(main.extract_seat_availability(self.page), {
            'available': [17], 'unavailable': [18, 19], 'unknown': []})
        for html in ('<p>loading</p>', '<table><tr><td class="vwSeatTd17">?</td></tr></table>'):
            self.page.set_content(html)
            with self.assertRaises(main.ParseError):
                main.extract_seat_availability(self.page)

    def test_closed_and_ambiguous_schedule(self):
        self.page.set_content('<table class="bus_table2"><tbody><tr><td>06:30</td><td>마감</td></tr></tbody></table>')
        with self.assertRaises(main.ScheduleClosed):
            main.select_schedule_row_by_time(self.page, '06')
        self.page.set_content('''<table class="bus_table2"><tbody>
        <tr><td>06:00</td><td><button>예약</button></td></tr>
        <tr><td>06:30</td><td><button>예약</button></td></tr></tbody></table>''')
        with self.assertRaises(main.ConfigError):
            main.select_schedule_row_by_time(self.page, '06')
        self.assertEqual(main.select_schedule_row_by_time(self.page, '06:30')[1], '06:30')

    def test_diagnostics_scrub_input_and_failure_is_best_effort(self):
        self.page.set_content('<input value="hidden-value"><script>const token="secret";</script><a href="https://x/?token=abc">link</a>')
        with tempfile.TemporaryDirectory() as folder, patch('main.ARTIFACT_DIR', Path(folder)):
            main.save_diagnostics(self.page, 1)
            html = (Path(folder) / 'attempt_1.html').read_text()
            for value in ('hidden-value', 'secret', 'abc'):
                self.assertNotIn(value, html)
        with tempfile.TemporaryDirectory() as folder, patch('main.ARTIFACT_DIR', Path(folder)), patch('main.log'):
            bad_page = Mock()
            bad_page.locator.side_effect = RuntimeError('closed page')
            main.save_diagnostics(bad_page, 1)


class FullFlowTests(unittest.TestCase):
    def test_fixture_login_to_seats(self):
        original = Browser.new_context
        visited = []
        def create_context(browser, *args, **kwargs):
            context = original(browser, *args, **kwargs)
            def serve(route):
                url = route.request.url
                visited.append(url)
                if url.endswith('/login.html'):
                    html = '<form method="post" action="/rsvc/"><input id="m_id"><input type="password"><button>로그인</button></form>'
                elif url.endswith('/rsvc/') and route.request.method == 'GET':
                    html = '<a href="/rsvc/login.html">로그인</a>'
                elif url.endswith('/rsvc/'):
                    html = '''<input type="radio" id="ln_direct1" checked>
                    <select name="ln_idx"><option value="105">부산</option></select>
                    <button onclick="location.href='/rsvc/schedule.html'">조회</button>'''
                elif url.endswith('/schedule.html'):
                    html = '''<table class="bus_table2"><tbody><tr><td>06:30</td><td>
                    <button onclick="location.href='/rsvc/boarding.html'">예약</button></td></tr></tbody></table>'''
                elif url.endswith('/boarding.html'):
                    html = '''<table><tr><td>덕천역</td><td><input type="radio"
                    onclick="location.href='/rsvc/seats.html'"></td></tr></table>'''
                elif url.endswith('/seats.html'):
                    html = '<div id="selSeatNum"><table><tr><td class="vwSeatTd17"><input type="checkbox"></td></tr></table></div>'
                else:
                    route.abort()
                    return
                route.fulfill(body=html, content_type='text/html; charset=utf-8')
            context.route('**/*', serve)
            return context
        cfg = dict(direction='in', line_keyword='부산', dispatch_time_kw='06', board_station_kw='덕천역')
        with tempfile.TemporaryDirectory() as folder, patch('main.ARTIFACT_DIR', Path(folder)), \
             patch.object(Browser, 'new_context', new=create_context), patch('main.log'), \
             patch.dict(os.environ, {'BUS_USERNAME': 'fixture-user', 'BUS_PASSWORD': 'fixture-password',
                                     'LH_BUSRO_HEADLESS': 'true', 'LH_BUSRO_DEBUG_DUMP': 'false'}), \
             patch('main.requests.post') as post:
            result = main.run_attempt(cfg, 1)
        self.assertEqual(result.status, 'checked')
        self.assertEqual(result.seats['available'], [17])
        self.assertTrue(any(url.endswith('/seats.html') for url in visited))
        post.assert_not_called()


class RuntimeTests(unittest.TestCase):
    def test_lock_prevents_overlap(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '.lock'
            with runtime.run_lock(path) as first:
                with runtime.run_lock(path) as second:
                    self.assertTrue(first)
                    self.assertFalse(second)
            with runtime.run_lock(path) as third:
                self.assertTrue(third)

    def test_retention_does_not_remove_unrelated_files(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            stale = directory / '20260101_000000.log'
            scheduler_stale = directory / 'scheduler-20260101.log'
            keep = directory / 'notes.txt'
            fresh = directory / '20260922_000000.log'
            for path in (stale, scheduler_stale, keep, fresh):
                path.write_text('test')
            for path in (stale, scheduler_stale, keep):
                os.utime(path, (1, 1))
            runtime.prune_logs(directory)
            self.assertFalse(stale.exists())
            self.assertFalse(scheduler_stale.exists())
            self.assertTrue(keep.exists())
            self.assertTrue(fresh.exists())

    def test_timeout_terminates_worker(self):
        with tempfile.TemporaryDirectory() as folder, patch('main.notify_failure') as notify, redirect_stdout(io.StringIO()):
            root = Path(folder)
            (root / 'main.py').write_text('import time\ntime.sleep(30)\n')
            started = time.monotonic()
            self.assertEqual(runtime.supervise(root, '20260101_test', no_notify=True, worker_timeout=0.1), 1)
            self.assertLess(time.monotonic() - started, 5)
            notify.assert_called_once()
            self.assertIn('시간 제한', (root / 'logs/20260101_test.log').read_text())


class SchedulerTests(unittest.TestCase):
    def test_background_start_duplicate_stop_and_stale_state(self):
        with tempfile.TemporaryDirectory() as folder, redirect_stdout(io.StringIO()):
            root = Path(folder)
            (root / 'runtime.py').write_text('''from pathlib import Path
import time
Path('runs.txt').open('a').write('run\\n')
time.sleep(30)
''')
            logs, _, socket_path, state_path = manage_schedule.paths(root)
            logs.mkdir()
            state_path.write_text('{"token":"stale","pid":12345}')
            socket_path.touch()
            started = manage_schedule.start(root, no_notify=True, interval=0.3)
            self.assertEqual(started, 0, (logs / f"scheduler-{time.strftime('%Y%m%d')}.log").read_text())
            try:
                status = manage_schedule.request(root, 'status')
                self.assertEqual(status['status'], 'running')
                self.assertTrue(status['no_notify'])
                self.assertEqual(manage_schedule.start(root, no_notify=False, interval=0.3), 0)
                self.assertEqual(manage_schedule.request(root, 'status')['pid'], status['pid'])
                deadline = time.monotonic() + 4
                while not (root / 'runs.txt').exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue((root / 'runs.txt').exists())
                self.assertEqual(manage_schedule.stop(root), 0)
                self.assertIsNone(manage_schedule.request(root, 'status'))
                self.assertFalse(socket_path.exists())
                self.assertFalse(state_path.exists())
                self.assertEqual((root / 'runs.txt').read_text(), 'run\n')
            finally:
                manage_schedule.stop(root)


if __name__ == '__main__':
    unittest.main()
