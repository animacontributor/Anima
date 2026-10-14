"""Pattern naming panel with fake Handy HTTP; never moves a physical device."""
import json
from pathlib import Path
import sys
import threading
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_handy import HandyTests, handy, panel, config
from playwright.sync_api import sync_playwright


def main():
    fixture = HandyTests('test_all_patterns_are_listed_read_only_in_both_documented_shapes')
    fixture.setUp()
    server = None
    try:
        fixture.device.patterns[0]['name'] = '<img src=x onerror=window.patternExecuted=true>'
        state = {'welcome': False, 'version': 'test', 'folder': 'test', 'user_name': 'Fixture',
                 'tabs': {'Main': [], 'Senses': []}, 'senses': [], 'doors': {},
                 'secrets': {}, 'links': {}, 'brain': {'models': []}}
        with patch.object(panel, 'state', return_value=state):
            server = panel.ThreadingHTTPServer(('127.0.0.1', 0), panel.Handler)
            with patch.object(panel, 'PORT', server.server_address[1]):
                thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True)
                    page = browser.new_page(viewport={'width': 1100, 'height': 900})
                    errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
                    base = f'http://127.0.0.1:{server.server_port}'
                    def load():
                        page.goto(base + '/settings')
                        page.get_by_role('button', name='Senses', exact=True).click()
                        page.get_by_text('Pattern names and choices', exact=True).click()
                        page.get_by_role('button', name='Load Handy patterns', exact=True).click()
                        page.wait_for_function('document.querySelectorAll(".handy-pattern").length === 10')
                    load()
                    assert page.locator('.handy-pattern input[type=checkbox]:checked').count() == 10
                    assert page.locator('.handy-pattern img').count() == 0
                    assert page.evaluate('window.patternExecuted') is None
                    page.get_by_label('Pattern name for slot 0', exact=True).fill('Gentle waves')
                    page.get_by_label('Pattern description for slot 0', exact=True).fill('Slow changes in rhythm')
                    page.get_by_label('Available to the friend for slot 1', exact=True).uncheck()
                    page.get_by_role('button', name='Save pattern names and choices', exact=True).click()
                    page.wait_for_function('document.getElementById("banner").textContent.includes("Pattern names and availability saved")')
                    load()
                    assert page.get_by_label('Pattern name for slot 0', exact=True).input_value() == 'Gentle waves'
                    assert page.get_by_label('Pattern description for slot 0', exact=True).input_value() == 'Slow changes in rhythm'
                    assert not page.get_by_label('Available to the friend for slot 1', exact=True).is_checked()
                    patterns = handy.list_device_patterns()['patterns']
                    assert len(patterns) == 9 and patterns[0]['name'] == 'Gentle waves'
                    assert all(c[0] == 'GET' for c in fixture.device.calls)
                    page.get_by_label('Pattern test playback rate',exact=True).fill('0.5')
                    page.get_by_label('Pattern test amplitude',exact=True).fill('65')
                    # Keeper tests remain possible after excluding a pattern
                    # from the friend's choices; no names/settings need to be saved.
                    page.get_by_role('button',name='Test pattern in slot 1',exact=True).click()
                    page.wait_for_function('document.querySelector("[role=status]").textContent.includes("Playing Pattern 1")')
                    assert fixture.device.pattern['id']==101 and fixture.device.running
                    assert (fixture.device.rate,fixture.device.amplitude)==(.5,.65)
                    page.get_by_role('button',name='Stop Handy',exact=True).click()
                    page.wait_for_function('document.querySelector("[role=status]").textContent === "Handy stopped."')
                    assert not fixture.device.running
                    assert all('slider' not in c[1] and 'stroke' not in c[1] for c in fixture.device.calls)
                    # A delayed Play HTTP response must not disable Stop or
                    # overwrite its newer stopped result when it arrives.
                    held=[]
                    def hold_test(route):
                        held.append((route,route.fetch()))
                    page.route('**/api/handy/pattern-test',hold_test)
                    page.get_by_role('button',name='Test pattern in slot 0',exact=True).click()
                    page.wait_for_function('document.querySelector("[role=status]").textContent === "Starting pattern…"')
                    deadline=time.monotonic()+3
                    while not held and time.monotonic()<deadline:page.wait_for_timeout(20)
                    assert held and fixture.device.running
                    assert page.get_by_role('button',name='Test pattern in slot 1',exact=True).is_disabled()
                    assert not page.get_by_role('button',name='Stop Handy',exact=True).is_disabled()
                    page.get_by_role('button',name='Stop Handy',exact=True).click()
                    page.wait_for_function('document.querySelector("[role=status]").textContent === "Handy stopped."')
                    held[0][0].fulfill(response=held[0][1])
                    page.wait_for_function("""!document.querySelector('[aria-label="Test pattern in slot 0"]').disabled""")
                    assert not fixture.device.running
                    assert page.locator('[role=status]').inner_text()=='Handy stopped.'
                    page.unroute('**/api/handy/pattern-test',hold_test)
                    artifact = config.ROOT / '.runtime' / 'handy-panel-check'
                    artifact.mkdir(parents=True, exist_ok=True)
                    page.get_by_text('Pattern names and choices', exact=True).scroll_into_view_if_needed()
                    page.screenshot(path=str(artifact/'desktop.png'), full_page=True)
                    page.set_viewport_size({'width': 390, 'height': 844})
                    page.screenshot(path=str(artifact/'mobile.png'), full_page=True)
                    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                    page.get_by_role('button',name='Test pattern in slot 9',exact=True).scroll_into_view_if_needed()
                    stop=page.get_by_role('button',name='Stop Handy',exact=True).bounding_box()
                    assert stop and 0 <= stop['y'] <= 844-stop['height'], 'Stop scrolled out of view'
                    assert errors == [], errors
                    browser.close()
    finally:
        if server:
            server.shutdown(); server.server_close(); thread.join(2)
        fixture.doCleanups()
    print('PASS pattern panel: editable names/descriptions, saved exclusions, safe labels, manual play/stop with bounded settings and unchanged slider limits, mobile layout')


if __name__ == '__main__':
    main()
