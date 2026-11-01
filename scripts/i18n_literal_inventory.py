"""Inventory Chinese literals to review; it does not edit source or translate data."""
from pathlib import Path
import re,json
WEB=Path(__file__).resolve().parents[1]/'web'
pat=re.compile(r'''(['"])((?:\\.|(?!\1).)*?)\1''',re.S)
for name,page in [('app.js','index.html'),('chat.js','chat.html'),('activity-story.js','activity.html')]:
 text=(WEB/name).read_text(encoding='utf8');d=json.loads(re.search(r'id="sg-page-i18n">(.*?)</script>',(WEB/page).read_text(encoding='utf8'),re.S).group(1));keys=[]
 for m in pat.finditer(text):
  x=m.group(2)
  if re.search('[\u4e00-\u9fff]',x) and '\n' not in x and x not in d and x not in keys:keys.append(x)
 print('===',name,len(keys))
 for x in keys:print(x)
