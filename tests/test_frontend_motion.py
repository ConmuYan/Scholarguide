"""Cohesive surfaces and real motion, using temporary offline browser fixtures."""
import os
from pathlib import Path
import sys
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import test_experience_upgrade as harness
from selenium.webdriver.common.by import By

class FrontendMotionTests(unittest.TestCase):
    setUpClass=classmethod(harness.ExperienceUpgradeTests.setUpClass.__func__)
    tearDownClass=classmethod(harness.ExperienceUpgradeTests.tearDownClass.__func__)
    inject=harness.ExperienceUpgradeTests.inject
    def setUp(self):
        harness.ExperienceUpgradeTests.setUp(self)
        self.media('no-preference')
    def tearDown(self):
        harness.ExperienceUpgradeTests.tearDown(self)
        self.driver.execute_cdp_cmd('Emulation.setEmulatedMedia',{'features':[]})
    def media(self,value):
        self.driver.execute_cdp_cmd('Emulation.setEmulatedMedia',{'features':[{'name':'prefers-reduced-motion','value':value}]})
    def viewport(self,width,height=1000):
        self.driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride',{'width':width,'height':height,'deviceScaleFactor':1,'mobile':False})
    def open(self,path):
        self.driver.get(self.base+path)
        self.wait.until(lambda d:d.find_elements(By.ID,'sg-language-toggle'))
    def shot(self,name,selector=None):
        output=os.environ.get('SG_MOTION_SHOTS')
        if not output:return
        folder=Path(output);folder.mkdir(parents=True,exist_ok=True)
        if selector:self.driver.find_element(By.CSS_SELECTOR,selector).screenshot(str(folder/(name+'.png')))
        else:self.driver.save_screenshot(str(folder/(name+'.png')))

    def test_paper_palette_and_wide_alignment(self):
        self.media('reduce');self.open('/search')
        self.assertEqual(self.driver.find_elements(By.CSS_SELECTOR,'.sidebar-motif, .sidebar-motto, .sidebar-rule'),[])
        for lang in ('zh-CN','zh-Hant','en'):
            self.driver.execute_script('SGI18n.set(arguments[0])',lang)
            for width in (1440,1920,2382):
                self.viewport(width,1194 if width==2382 else 1000)
                for theme in ('light','dark'):
                    info=self.driver.execute_script('''document.documentElement.dataset.theme=arguments[0];
                      const side=document.querySelector('.sidebar'),page=document.querySelector('.page');
                      const rgb=e=>getComputedStyle(e).backgroundColor.match(/[\\d.]+/g).slice(0,3).map(Number);
                      const a=rgb(side),b=rgb(document.body),s=side.getBoundingClientRect(),p=page.getBoundingClientRect();
                      return {colorDelta:Math.max(...a.map((n,i)=>Math.abs(n-b[i]))),
                        radius:getComputedStyle(side).borderRadius,left:s.left,width:s.width,
                        gap:p.left+parseFloat(getComputedStyle(page).paddingLeft)-s.right,
                        overflow:document.documentElement.scrollWidth-document.documentElement.clientWidth};''',theme)
                    self.assertLessEqual(info['colorDelta'],12,(width,theme,info))
                    self.assertEqual(info['radius'],'0px')
                    self.assertEqual(info['left'],0)
                    self.assertLessEqual(info['gap'],50,(width,theme,info))
                    self.assertLessEqual(info['overflow'],1,(width,theme,info))
                    if lang=='zh-Hant' and width in (1440,2382):self.shot(f'workspace-{width}-{lang}-{theme}')

    def test_source_capability_grid_across_languages_and_sizes(self):
        self.media('reduce');self.open('/')
        self.assertEqual(self.driver.find_elements(By.CSS_SELECTOR,'.lp-register-tag'),[])
        for lang in ('zh-CN','zh-Hant','en'):
            self.driver.execute_script('SGI18n.set(arguments[0])',lang)
            for width in (1440,1024,390,320):
                self.viewport(width)
                for theme in ('light','dark'):
                    self.driver.execute_script('''document.documentElement.dataset.theme=arguments[0];
                      document.querySelector('.lp-register').scrollIntoView({block:'center',behavior:'instant'});''',theme)
                    info=self.driver.execute_script('''const host=document.querySelector('.lp-register');
                      return {cards:[...host.children].map(e=>{const r=e.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height,overflow:e.scrollWidth-e.clientWidth}}),
                        columns:getComputedStyle(host).gridTemplateColumns.split(' ').length,
                        border:getComputedStyle(host).borderBottomStyle,
                        overflow:document.documentElement.scrollWidth-document.documentElement.clientWidth};''')
                    self.assertEqual(len(info['cards']),4)
                    self.assertEqual(info['columns'],2 if width>740 else 1,(lang,width,info))
                    self.assertEqual(info['border'],'none')
                    self.assertLessEqual(info['overflow'],1,(lang,width,theme,info))
                    for card in info['cards']:self.assertLessEqual(card['overflow'],1,(lang,width,card))
                    if width>740:
                        self.assertAlmostEqual(info['cards'][0]['y'],info['cards'][1]['y'],delta=1)
                        self.assertGreater(info['cards'][2]['y'],info['cards'][0]['y'])
                    else:self.assertGreater(info['cards'][1]['y'],info['cards'][0]['y'])
                    if lang in ('zh-Hant','en') and width in (1440,390):self.shot(f'sources-{width}-{lang}-{theme}','.lp-register')

    def test_controls_have_real_feedback_and_reduce_motion_cancels_it(self):
        self.open('/search')
        self.wait.until(lambda d:d.execute_script('return state.configLoaded && !state.configLoading'))
        result=self.driver.execute_script('''
          document.querySelector('#topic').value='Keep my original request';
          window.motionFetches=0;const original=window.fetch;
          window.fetch=(...args)=>{motionFetches++;return original(...args)};
          const language=document.querySelector('#sg-language-toggle');language.click();
          const badge=language.querySelector('.sg-language-badge');
          const languageMotion=badge.getAnimations().some(a=>a.effect.getTiming().duration===180);
          const theme=document.querySelector('#theme-toggle');theme.click();
          return {languageMotion,themeMotion:theme.getAnimations().some(a=>a.effect.getTiming().duration===220),
            request:document.querySelector('#topic').value,calls:motionFetches,mode:document.documentElement.dataset.motion};
        ''')
        self.assertEqual(result,{'languageMotion':True,'themeMotion':True,'request':'Keep my original request','calls':0,'mode':'full'})
        self.media('reduce')
        self.wait.until(lambda d:d.execute_script('return document.documentElement.dataset.motion==="reduced"'))
        reduced=self.driver.execute_script('''
          const theme=document.querySelector('#theme-toggle');theme.click();SGI18n.set('en');
          return {theme:theme.getAnimations().length,badge:document.querySelector('.sg-language-badge').getAnimations().length,
            header:getComputedStyle(document.querySelector('.page-head')).animationName,calls:motionFetches};
        ''')
        self.assertEqual(reduced,{'theme':0,'badge':0,'header':'none','calls':0})

    def test_native_navigation_remains_unintercepted(self):
        from urllib.parse import urlsplit
        self.open('/search')
        self.driver.find_element(By.CSS_SELECTOR,'.side-link[href="/chat"]').click()
        self.wait.until(lambda d:urlsplit(d.current_url).path=='/chat' and d.find_elements(By.ID,'sg-language-toggle'))
        self.assertEqual(self.driver.find_element(By.CSS_SELECTOR,'.side-link.active').get_attribute('href'),self.base+'/chat')

    def test_landing_scroll_reveals_are_once_per_section(self):
        self.inject('''window.revealCalls=[];const originalAnimate=Element.prototype.animate;
          Element.prototype.animate=function(frames,options){if(this.classList.contains('lp-register-group'))
            revealCalls.push({index:[...document.querySelectorAll('.lp-register-group')].indexOf(this),duration:options.duration,fill:options.fill});
            return originalAnimate.call(this,frames,options)};''')
        self.open('/')
        self.driver.execute_script("document.querySelector('.lp-register').scrollIntoView({block:'center',behavior:'instant'})")
        self.wait.until(lambda d:d.execute_script('return revealCalls.length>=2'))
        calls=self.driver.execute_script('return revealCalls')
        self.assertTrue(all(row['duration']==380 for row in calls))
        self.assertTrue(all(row['fill']=='backwards' for row in calls))
        self.assertEqual(len({row['index'] for row in calls}),len(calls))
        self.driver.execute_script("window.scrollTo({top:0,behavior:'instant'})")
        self.driver.execute_async_script('const done=arguments[0];requestAnimationFrame(()=>requestAnimationFrame(done))')
        self.driver.execute_script("document.querySelector('.lp-register').scrollIntoView({block:'center',behavior:'instant'})")
        self.driver.execute_async_script('const done=arguments[0];requestAnimationFrame(()=>requestAnimationFrame(done))')
        self.assertEqual(self.driver.execute_script('return revealCalls'),calls)

if __name__=='__main__':unittest.main()
