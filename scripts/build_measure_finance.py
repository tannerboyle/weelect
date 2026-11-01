"""Build data/measure_finance.json from each measure's Ballotpedia campaign-finance section.

Usage: python scripts/build_measure_finance.py CACHE_DIR [--fetch]
  CACHE_DIR holds one saved Ballotpedia page per measure (CACHE_DIR/<measure id>.html); --fetch downloads them first.
Needs beautifulsoup4 + lxml (dev only, not in requirements.txt). Copies figures exactly as Ballotpedia shows them;
exact-duplicate donor names in one donor table are added together (recorded as "rows": n). Review the diff by hand
before committing: Ballotpedia's layout varies by state.
"""
import json,re,glob,html,os,sys,subprocess,time
from bs4 import BeautifulSoup
REPO=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE=sys.argv[1] if len(sys.argv)>1 else 'bp'
items=[]
for f in ['data/measures_bothsides.json','data/measures_bothsides_b.json']:
    items+=json.load(open(f'{REPO}/{f}'))['items']
PORTAL={
 'AZ':('Arizona Secretary of State, See the Money','https://seethemoney.az.gov/'),
 'CA':('California Secretary of State, Cal-Access','https://cal-access.sos.ca.gov/Campaign/Measures/'),
 'CO':('Colorado Secretary of State, TRACER','https://tracer.sos.colorado.gov/PublicSite/homepage.aspx'),
 'FL':('Florida Division of Elections, committee search','https://dos.elections.myflorida.com/committees/'),
 'MA':('Massachusetts Office of Campaign and Political Finance (OCPF)','https://www.ocpf.us/Reports/ActiveBallotQuestionReports'),
 'MI':('Michigan Bureau of Elections, committee search','https://mi-boe.entellitrak.com/etk-mi-boe-prod/page.request.do?page=page.miboeCommitteePublicSearch'),
 'MO':('Missouri Ethics Commission, campaign finance search','https://www.mec.mo.gov/mec/campaign_finance/CFSearch.aspx'),
 'NV':('Nevada Secretary of State, campaign finance search','https://www.nvsos.gov/SoSCandidateServices/AnonymousAccess/CEFDSearchUU/Search.aspx'),
 'NC':('North Carolina State Board of Elections, campaign finance','https://cf.ncsbe.gov/CFOrgLkup/'),
 'OH':('Ohio Secretary of State, campaign finance','https://www.ohiosos.gov/campaign-finance/search/'),
 'VA':('Virginia Department of Elections, campaign finance reports','https://cfreports.elections.virginia.gov/'),
 'WA':('Washington Public Disclosure Commission','https://www.pdc.wa.gov/political-disclosure-reporting-data/browse-search-data/committees'),
}
MONTHS='January February March April May June July August September October November December'.split()
def iso(d):
    m=re.match(r'([A-Z][a-z]+) (\d+) ?, (\d{4})',d); return f"{m.group(3)}-{MONTHS.index(m.group(1))+1:02d}-{int(m.group(2)):02d}"
def money(s):
    s=s.strip().replace('$','').replace(',','')
    try: return round(float(s),2)
    except: return None
def section(soup):
    a=soup.find(id='Campaign_finance'); h=a.find_parent('h2') or a
    els=[]
    for el in h.find_all_next():
        if el.name=='h2': break
        els.append(el)
    return els
def parse(it):
    soup=BeautifulSoup(open(os.path.join(CACHE,it['id']+".html")).read(),'lxml')
    els=section(soup)
    text=re.sub(r'\s+',' ',' '.join(e.get_text(' ',strip=True) for e in els if e.name in('p','div','ul','dl') and e.find_parent('table') is None))
    m=re.search(r'covered through ([A-Z][a-z]+ \d+ ?, \d{4})',text)
    asof=iso(m.group(1)) if m else None
    sides={'yes':{'committees':[],'donors':[],'total':None,'ie':[]},'no':{'committees':[],'donors':[],'total':None,'ie':[]}}
    side=None; got_summary=False; stop=False
    for el in els:
        if el.name in('h3','h4','h5'):
            t=el.get_text(' ',strip=True).lower()
            if (re.match(r'^20\d\d$',t) and t!='2026') or t.startswith('methodology'): stop=True
            if t.startswith('support'): side='yes'
            elif t.startswith('oppos'): side='no'
        if stop: break
        if el.name=='table':
            rows=[[c.get_text(' ',strip=True) for c in tr.find_all(['td','th'])] for tr in el.find_all('tr')]
            hdr=' '.join(' '.join(r) for r in rows[:2]).lower()
            if 'donor' in hdr and side:
                for r in rows:
                    if len(r)>=4 and r[0]!='Donor' and money(r[3]) is not None:
                        sides[side]['donors'].append({'name':re.sub(r'\s+',' ',r[0]),'amount':money(r[3])})
            elif 'committee' in hdr and side:
                for r in rows:
                    if len(r)>=4 and r[0] not in ('Total','Committee') and not r[0].startswith('Committees in') and money(r[3]) is not None:
                        sides[side]['committees'].append({'name':re.sub(r'\s+',' ',r[0]),'total':money(r[3])})
            elif 'total contributions' in hdr and not got_summary:
                got_summary=True
                for r in rows:
                    if r and r[0]=='Support': sides['yes']['total']=money(r[3])
                    if r and r[0]=='Oppose': sides['no']['total']=money(r[3])
    flat=re.sub(r'\[\s*\d+\s*\]','',text)
    for mm in re.finditer(r'(?:made|reported a total of \$[\d,\.]+ in) independent expenditures (?:to )?(support|oppos)\w* Proposition \d+[:.]?(.*?)(?=(?:The following|Opposition|Support|Methodology|$))',flat):
        sd='yes' if mm.group(1)=='support' else 'no'
        seg=mm.group(0)
        for p in re.finditer(r'((?:[Tt]he )?[A-Z][\w&\'\(\)\.\- ]*?) (?:spent a total of|made independent expenditures totaling|reported a total of) \$([\d,\.]+\d)(?: in independent expenditures)?(?: to (?:support|oppose) Proposition \d+)?(?: ((?:in|from) [A-Za-z ]*?\d{4}))?',seg):
            nm=re.sub(r'^(?:and |the )','',p.group(1).strip(),flags=re.I); nm=re.sub(r'^(?:[Tt]he )','',nm)
            rec={'name':nm,'amount':money(p.group(2)),'period':(p.group(3) or '').strip()}
            if rec not in sides[sd]['ie']: sides[sd]['ie'].append(rec)
    # merge exact duplicate donor names
    for s in sides.values():
        merged={}
        for d in s['donors']:
            if d['name'] in merged:
                merged[d['name']]['amount']=round(merged[d['name']]['amount']+d['amount'],2); merged[d['name']]['rows']=merged[d['name']].get('rows',1)+1
            else: merged[d['name']]=dict(d)
        s['donors']=sorted(merged.values(),key=lambda d:-d['amount'])
        if not s['committees']: s['total']=None if not s['total'] else s['total']
    # committee links from refs
    refs={}
    for li in soup.select('ol.references li'):
        a=[x.get('href') for x in li.find_all('a') if x.get('href','').startswith('http')]
        t=li.get_text(' ',strip=True)
        mm=re.search(r'"([^"]+)"',t)
        if a and mm: refs[mm.group(1).lower()]=a[0]
    for s in sides.values():
        for c in s['committees']:
            for k,u in refs.items():
                kk=re.sub(r' campaign finance$','',k)
                if any(x in u for x in ('pdc.wa.gov/','tracer.sos','ComDetail','Committees/Detail','ocpf.us/filers')) and (kk==c['name'].lower() or kk.startswith(c['name'].lower()) or c['name'].lower().startswith(kk)):
                    c['url']=u
    return asof,sides,text
if '--fetch' in sys.argv:
    os.makedirs(CACHE,exist_ok=True)
    for it in items:
        subprocess.run(['curl','-sL','-A','Mozilla/5.0','-o',os.path.join(CACHE,it['id']+'.html'),it['ballotpedia_url']]); time.sleep(1)
out={'meta':{'source_note':'Figures are reported contributions to registered ballot measure committees, as compiled by Ballotpedia from state campaign-finance filings. They change as new reports are filed.','threshold':100000,'min_shown':3,'fallback_top':5,'built':'2026-11-01'},'items':{}}
for it in items:
    asof,sides,text=parse(it)
    st=it['state']
    out['items'][it['id']]={'as_of':asof,'source':{'org':'Ballotpedia','title':f"{it['state_name']} {it['label']} (2026): Campaign finance",'url':it['ballotpedia_url']+'#Campaign_finance','accessed':'2026-11-01'},
      'filings':{'org':PORTAL[st][0],'url':PORTAL[st][1]},'yes':sides['yes'],'no':sides['no']}
# shared committees
from collections import defaultdict
seen=defaultdict(set)
for k,v in out['items'].items():
    for s in('yes','no'):
        for c in v[s]['committees']: seen[c['name']].add(k)
for k,v in out['items'].items():
    for s in('yes','no'):
        for c in v[s]['committees']:
            others=sorted(seen[c['name']]-{k})
            if others: c['also']=others
# Known parser gap: this sentence form names the spender before the matched phrase (checked by hand 2026-11-01).
if not out['items']['arizona-proposition-319']['yes']['ie']:
    out['items']['arizona-proposition-319']['yes']['ie']=[{'name':'Arizona Free Enterprise Club','amount':4639.65,'period':''}]
json.dump(out,open(os.path.join(REPO,'data','measure_finance.json'),'w'),indent=1,ensure_ascii=False)
for k,v in out['items'].items():
    if '-v' not in sys.argv: continue
    print(k,v['as_of'],'Y',v['yes']['total'],len(v['yes']['committees']),len(v['yes']['donors']),'IE',v['yes']['ie'],'| N',v['no']['total'],len(v['no']['committees']),len(v['no']['donors']),'IE',v['no']['ie'])
    for s in('yes','no'):
        for c in v[s]['committees']:
            if 'url' in c: print('    url',c['name'],c['url'])
        for d in v[s]['donors']:
            if d.get('rows'): print('    merged',d)
