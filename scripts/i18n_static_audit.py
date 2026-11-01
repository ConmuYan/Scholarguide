from bs4 import BeautifulSoup
from pathlib import Path
import re,json
web=Path(__file__).resolve().parents[1]/'web'
for p in web.glob('*.html'):
 s=BeautifulSoup(p.read_text(encoding='utf8'),'html.parser');missing=[]
 for n in s.find_all(string=True):
  if not re.search('[\u4e00-\u9fff]',str(n)) or n.parent.name in ('script','style'):continue
  if not n.strip():continue
  # Only the actual leaf can own this text. Ancestor markers are not sufficient.
  if n.parent.has_attr('data-i18n'): continue
  missing.append((n.parent.name,n.parent.get('id',''),str(n).strip()))
 print(p.name,len(missing))
 for row in missing:print(row)
