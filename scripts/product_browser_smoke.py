"""Read-only smoke of the running product, with real Chrome screenshots."""
import json
from pathlib import Path
import sys
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait


def main():
    base = sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:8766'
    output = Path(sys.argv[2]) if len(sys.argv) > 2 else Path('runs/upgrade-20261028/browser-live')
    output.mkdir(parents=True, exist_ok=True)
    options = Options()
    for flag in ('--headless=new', '--no-sandbox', '--disable-gpu', '--hide-scrollbars'):
        options.add_argument(flag)
    options.set_capability('goog:loggingPrefs', {'browser': 'ALL'})
    driver = webdriver.Chrome(options=options)
    records = []
    try:
        driver.get(base + '/settings')
        for width in (1440, 390):
            driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride', {'width': width, 'height': 1000, 'deviceScaleFactor': 1, 'mobile': False})
            for theme in ('light', 'dark'):
                driver.execute_script("localStorage.setItem('scholarguide-theme',arguments[0])", theme)
                for name, route, selector in (('sources', '/sources', '#sources-list .source-row'), ('search', '/search', '#search-form'), ('chat', '/chat', '#composer'), ('activity','/activity','#activity-list')):
                    driver.get(base + route)
                    WebDriverWait(driver, 12).until(lambda d: d.find_elements(By.CSS_SELECTOR, selector))
                    size = driver.execute_script('return {width:document.documentElement.clientWidth,scroll:document.documentElement.scrollWidth}')
                    errors = [row for row in driver.get_log('browser') if row['level'] == 'SEVERE']
                    record = {'page': name, 'width': width, 'theme': theme, 'dimensions': size, 'errors': errors}
                    records.append(record)
                    driver.save_screenshot(str(output / f'{name}-{width}-{theme}.png'))
                    if size['scroll'] > size['width'] or errors:
                        raise AssertionError(record)
        (output / 'summary.json').write_text(json.dumps({'passed': True, 'records': records}, indent=2), encoding='utf-8')
        print(json.dumps({'passed': True, 'screenshots': len(records)}))
    except Exception:
        (output / 'summary.json').write_text(json.dumps({'passed': False, 'records': records}, indent=2), encoding='utf-8')
        raise
    finally:
        driver.quit()


if __name__ == '__main__':
    main()
