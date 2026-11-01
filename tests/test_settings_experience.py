"""Real-browser provider editing journeys; probes are local fetch fixtures."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_ui_browser as browser_support


class SettingsExperienceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        browser_support.BrowserUiTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        browser_support.BrowserUiTests.tearDownClass.__func__(cls)

    def setUp(self):
        self.driver.set_window_size(1280, 1000)
        self.driver.get(self.base + '/settings')
        self.driver.execute_script('sessionStorage.clear(); localStorage.setItem("scholarguide-language", "zh-CN");')
        self.driver.refresh()
        self.wait = browser_support.WebDriverWait(self.driver, 10)
        self.wait.until(lambda d: d.find_elements(browser_support.By.CSS_SELECTOR, '#provider-list button'))
        self.click('#new-provider')

    def click(self, selector):
        self.driver.execute_script('arguments[0].click();', self.driver.find_element(browser_support.By.CSS_SELECTOR, selector))

    def value(self, element):
        return self.driver.find_element(browser_support.By.ID, element).get_attribute('value')

    def fill(self):
        for element, value in {'provider-name':'Draft endpoint', 'provider-url':'https://example.invalid/v1',
                               'provider-model':'fixture-model', 'provider-key':'secret-draft-only'}.items():
            self.driver.find_element(browser_support.By.ID, element).send_keys(value)

    def test_configuration_draft_restores_without_persisting_the_key(self):
        self.fill()
        self.assertNotIn('secret-draft-only', self.driver.execute_script('return JSON.stringify(sessionStorage)'))
        self.driver.refresh()
        self.wait.until(lambda d: d.find_element(browser_support.By.ID, 'provider-name').get_attribute('value') == 'Draft endpoint')
        self.assertEqual(self.value('provider-key'), '')
        self.assertIn('密钥', self.driver.find_element(browser_support.By.ID, 'provider-draft-status').text)
        self.driver.execute_script("fillForm(profileById('env')); renderList();")
        self.click('#new-provider')
        self.assertEqual(self.value('provider-model'), 'fixture-model')
        self.click('#discard-provider-draft')
        self.assertEqual(self.value('provider-name'), '')
        self.assertFalse(self.driver.find_element(browser_support.By.ID, 'provider-draft-status').is_displayed())

    def test_probe_prevents_concurrent_mutation_and_returns_useful_auth_guidance(self):
        self.fill()
        self.driver.execute_script("""
          const original = window.fetch.bind(window);
          window.fetch = (...args) => String(args[0]) === '/api/providers/test'
            ? new Promise(resolve => { window.finishProbe = () => resolve(new Response(JSON.stringify({
                ok:false,status:'auth',http_status:401,protocol:'chat_completions',error:'fixture rejected key',
                message:'Endpoint test failed.'}),{status:200})); }) : original(...args);
        """)
        self.click('#test-provider')
        self.wait.until(lambda d: d.execute_script('return state.busy'))
        for element in ('new-provider','save-provider','activate-provider','provider-name','provider-key'):
            self.assertFalse(self.driver.find_element(browser_support.By.ID, element).is_enabled())
        self.assertTrue(all(not node.is_enabled() for node in self.driver.find_elements(browser_support.By.CSS_SELECTOR, '#provider-list button')))
        self.driver.execute_script('finishProbe()')
        self.wait.until(lambda d: not d.execute_script('return state.busy'))
        self.assertIn('访问权限', self.driver.find_element(browser_support.By.ID, 'test-result').text)
        self.assertEqual(self.value('provider-key'), 'secret-draft-only')
        self.assertTrue(self.driver.find_element(browser_support.By.ID, 'save-provider').is_enabled())

    def test_editing_or_switching_provider_does_not_show_old_test_result(self):
        self.fill()
        self.driver.execute_script("""
          const original = window.fetch.bind(window);
          window.fetch = (...args) => String(args[0]) === '/api/providers/test'
            ? Promise.resolve(new Response(JSON.stringify({ok:true,status:'ok',
                protocol:'chat_completions',model:'fixture-model',message:'Fixture connected.'}),{status:200}))
            : original(...args);
        """)
        self.click('#test-provider')
        self.wait.until(lambda d: not d.execute_script('return state.busy'))
        self.assertTrue(self.driver.find_element(browser_support.By.ID, 'test-result').is_displayed())
        self.driver.find_element(browser_support.By.ID, 'provider-model').send_keys('-changed')
        self.assertFalse(self.driver.find_element(browser_support.By.ID, 'test-result').is_displayed())
        self.assertNotIn('Fixture connected', self.driver.find_element(browser_support.By.ID, 'settings-status').text)
        self.driver.execute_script("setStatus('Old profile result', 'ok');")
        self.click('#provider-list button')
        self.assertNotIn('Old profile result', self.driver.find_element(browser_support.By.ID, 'settings-status').text)

    def test_failed_save_keeps_editable_values_and_the_reloadable_configuration_draft(self):
        self.fill()
        self.driver.execute_script("""
          const original = window.fetch.bind(window);
          window.fetch = (...args) => String(args[0]) === '/api/providers' && args[1]?.method === 'POST'
            ? Promise.resolve(new Response(JSON.stringify({error:'fixture disk failure'}),{status:400}))
            : original(...args);
        """)
        self.click('#save-provider')
        self.wait.until(lambda d: 'fixture disk failure' in d.find_element(browser_support.By.ID,'settings-status').text)
        self.assertEqual(self.value('provider-name'), 'Draft endpoint')
        self.assertEqual(self.value('provider-key'), 'secret-draft-only')
        self.assertTrue(self.driver.find_element(browser_support.By.ID, 'save-provider').is_enabled())
        self.driver.refresh()
        self.wait.until(lambda d: d.find_element(browser_support.By.ID, 'provider-model').get_attribute('value') == 'fixture-model')
        self.assertEqual(self.value('provider-key'), '')

    def test_own_key_visibility_keyboard_save_and_activation_handoff(self):
        self.fill()
        self.click('#toggle-provider-key')
        self.assertEqual(self.driver.find_element(browser_support.By.ID,'provider-key').get_attribute('type'), 'text')
        self.assertEqual(self.driver.find_element(browser_support.By.ID,'toggle-provider-key').get_attribute('aria-pressed'), 'true')
        self.driver.find_element(browser_support.By.ID,'provider-model').send_keys(browser_support.Keys.CONTROL, browser_support.Keys.ENTER)
        self.wait.until(lambda d: '设为当前模型' in d.find_element(browser_support.By.ID,'settings-status').text)
        self.assertIn('Draft endpoint', self.driver.find_element(browser_support.By.ID,'active-model-label').text)
        self.assertEqual(self.value('provider-key'), '')
        self.assertNotIn('secret-draft-only', self.driver.page_source)
        self.assertTrue(self.driver.find_element(browser_support.By.ID,'provider-ready-actions').is_displayed())
        self.assertNotIn('new', self.driver.execute_script('return Object.keys(JSON.parse(sessionStorage.getItem(DRAFT_KEY)||"{}"))'))

    def test_environment_keyboard_and_programmatic_submit_cannot_clone_or_activate_a_blank_key(self):
        self.driver.execute_script("fillForm(profileById('env')); renderList(); window.__profileWrites=0; const original=window.fetch.bind(window); window.fetch=(...args)=>{ if(String(args[0])==='/api/providers' && args[1]?.method==='POST') window.__profileWrites++; return original(...args); };")
        self.driver.execute_script("document.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',ctrlKey:true,bubbles:true})); document.querySelector('#provider-form').dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));")
        self.assertEqual(self.driver.execute_script('return window.__profileWrites'), 0)
        self.assertEqual(self.driver.execute_script('return state.selected'), 'env')
        self.assertIn('只读', self.driver.find_element(browser_support.By.ID, 'settings-status').text)


if __name__ == '__main__':
    unittest.main()
