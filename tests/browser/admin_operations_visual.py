"""Synthetic browser fixture: real operation partials, CSS and navigation JS.
No application database, authentication session or external service is used.
Run manually with Playwright + Chromium installed:
OPS_VISUAL_OUTPUT=/tmp/ops-design python tests/browser/admin_operations_visual.py
This tests the real fragments/assets in a synthetic dashboard shell, not a live authenticated deployment.
"""
from pathlib import Path
from types import SimpleNamespace as NS
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlparse, parse_qs, urlencode
import json
import re
import os
from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(os.environ.get('OPS_VISUAL_OUTPUT', '/tmp/pollomodas-ops-design'))
OUT.mkdir(exist_ok=True)
TABS = {'orders':'Pedidos', 'manual-review':'Revisão manual', 'sales-readiness':'Prontidão para vendas', 'checkout-recovery':'Recuperação de checkout'}
env = Environment(loader=FileSystemLoader(ROOT/'loja/templates'), autoescape=select_autoescape())
def url_for(endpoint, **kw):
    if endpoint.startswith('admin.ops_partial_'):
        path = '/admin/painel/' + endpoint.split('ops_partial_')[1].replace('_','-')
    else:
        path = '/fixture/' + endpoint.split('.')[-1]
    return path + ('?' + urlencode(kw) if kw else '')
env.globals.update(url_for=url_for, csrf_token=lambda:'synthetic-csrf-no-action')
labels={'pending_payment':'Aguardando pagamento','paid':'Pago','manual_review':'Revisão manual','cancelled':'Cancelado'}
def order(i=1):
    return NS(id=f'synthetic-{i}', order_number=f'PM261009TEST{i:02}', total=Decimal('68.00'),
        customer_name='Cliente sintético com nome extenso para verificar a leitura completa no painel administrativo',
        customer_email='cliente.sintetico.muito.longo@example.invalid', created_at=datetime(2026,10,9,13,30),
        status='pending_payment' if i==1 else 'paid', stock_reserved=True,
        manual_review_reason='Pagamento informado, mas a variante foi desabilitada antes da confirmação. Conferir disponibilidade e justificativa antes de decidir.',
        mp_preference_expires_at=datetime(2026,10,9,20,30))
def render(tab, query, empty=False):
    search=query.get('q',[''])[0]; status=query.get('status',[''])[0]; n=int(query.get('page',['1'])[0])
    orders=[] if empty or search=='inexistente' else [order(), order(2)]
    if tab=='orders':
        ctx=dict(status_labels=labels,status_counts={'pending_payment':12,'paid':8,'manual_review':2,'cancelled':1},
          search=search,status=status,page=NS(items=orders,total=23 if orders else 0,page=n,has_prev=n>1,has_next=n<3,prev_num=n-1,next_num=n+1),
          payment_is_confirmed=lambda o:o.status=='paid')
    elif tab=='manual-review': ctx=dict(orders=orders)
    elif tab=='sales-readiness':
        rows=[NS(name='Vestido Sem Manga em Algodão com Cores Delicadas – Modelagem Leve com Toque Decorativo para Looks Frescos e Versáteis',
            origin='AliExpress',supplier_name='AliExpress',price_display='R$ 58,00',proven_cost_display='R$ 34,80',
            charged_freight_display='R$ 10,00',proven_logistic_display='R$ 0,00',cash_margin_display='14,16%',
            installment_margin_display='14,16%',margin_display='14,16%',stock=1111,state='PRONTO',reasons=['Tratamento manual obrigatório após pagamento']),
            NS(name='Produto sintético com bloqueios operacionais',origin='AliExpress',supplier_name='AliExpress',price_display='R$ 49,72',
            proven_cost_display='—',charged_freight_display='R$ 10,00',proven_logistic_display='—',cash_margin_display='—',
            installment_margin_display='—',margin_display='—',stock=0,state='BLOQUEADO',reasons=['Cotação de frete expirada','Tributos da compra não confirmados','Estoque indisponível'])]
        ctx=dict(reason_labels={},summary=NS(pronto=0 if empty else 1,revisao=0,bloqueado=0 if empty else 1,evaluated=0 if empty else 2,
          reference_configured=True,reference_masked='770•••••',candidates=[] if empty else rows[:1],rows=[] if empty else rows))
    else:
        for o in orders: o.status='pending_payment'
        ctx=dict(enabled=False,email_ready=False,after_hours=2,batch_enabled=False,result=None,
          candidates=orders,active_reservations=[] if empty else [NS(order_number=order().order_number,created_sp='09/10/2026 10:30',expires_sp='09/10/2026 17:30')],
          unproven_reservations_count=0,expired_reservations=[],reservation_hours=24,reservation_release_enabled=False,reservation_batch_release_enabled=False)
    return env.get_template('admin/partials/_'+tab.replace('-','_')+'.html').render(**ctx)
css=(ROOT/'public/static/styles/dashboard_admin.css').read_text()
js=(ROOT/'public/static/scripts/dashboard_admin.js').read_text()
nav=''.join(f'<button type="button" class="adm-nav-link" data-panel="{tab}"><span class="adm-icon">◈</span>{label}</button>' for tab,label in TABS.items())
panels='<section class="adm-panel active" id="panel-overview">Visão geral sintética</section>'+''.join(f'<section class="adm-panel" id="panel-{tab}"><div class="adm-lazy" data-partial-container="{tab}" data-partial-src="/admin/painel/{tab}"></div></section>' for tab in TABS)
html=f'''<!doctype html><html lang="pt-BR"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Operações — dados sintéticos</title><style>body{{margin:0}}{css}</style><div class="adm-wrap"><aside class="adm-sidebar"><div class="adm-sidebar-logo"><span>Admin</span>Pollo Modas</div><nav class="adm-nav"><div class="adm-nav-group-label">Operações</div>{nav}</nav></aside><div class="adm-main"><nav class="adm-mobile-nav">{nav}</nav><header class="adm-topbar"><div class="adm-topbar-title" id="adm-current-title">Painel</div><div id="adm-topbar-actions"></div></header><main class="adm-content">{panels}</main></div></div><script>{js}</script></html>'''

results=[]
with sync_playwright() as p:
    for width,height,mode in [(1440,1000,'desktop'),(390,844,'mobile')]:
        browser=p.chromium.launch(headless=True,args=['--no-sandbox','--single-process','--no-zygote'])
        page=browser.new_page(viewport={'width':width,'height':height})
        flags={'empty':False,'fail':False,'hold':False}; requests=[]; errors=[]; held=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        def route(r):
            u=urlparse(r.request.url)
            if u.path.startswith('/admin/painel/'):
                requests.append(u.path+('?' + u.query if u.query else ''))
                if flags['hold']:
                    held.append(r);return
                if flags['fail']:
                    flags['fail']=False;r.fulfill(status=503,body='Synthetic failure');return
                r.fulfill(content_type='text/html',body=render(u.path.rsplit('/',1)[1],parse_qs(u.query),flags['empty']))
            else:r.fulfill(content_type='text/html',body=html)
        page.route('**/*',route)
        for tab in TABS:
            page.goto('http://fixture.invalid/admin#'+tab)
            page.locator('[data-partial-root="'+tab+'"]').wait_for()
            assert page.locator('[data-panel="'+tab+'"].active').count()==2
            assert page.locator('[data-panel="'+tab+'"][aria-current="page"]').count()==2
            if mode=='mobile':
                selected=page.locator('.adm-mobile-nav [data-panel="'+tab+'"]').bounding_box()
                assert selected['x']>=0 and selected['x']+selected['width']<=width
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'),(mode,tab,'body overflow')
            page.screenshot(path=str(OUT/f'{tab}-{mode}.png'),full_page=True)
            if tab=='manual-review':
                assert page.locator('input[name=reason]').first.get_attribute('required') is not None
                page.locator('.adm-panel.active .adm-table-wrap').first.evaluate('(e)=>e.scrollLeft=e.scrollWidth')
                page.screenshot(path=str(OUT/f'{tab}-actions-{mode}.png'),full_page=True)
            # AA contrast for the small primary action labels.
            for button in page.locator('[data-partial-root] .adm-btn-primary').all():
                colors=button.evaluate('(e)=>[getComputedStyle(e).color,getComputedStyle(e).backgroundColor]')
                def luminance(color):
                    channels=[int(v)/255 for v in re.findall(r'\d+',color)[:3]]
                    values=[v/12.92 if v<=.04045 else ((v+.055)/1.055)**2.4 for v in channels]
                    return sum(v*w for v,w in zip(values,[.2126,.7152,.0722]))
                light,dark=sorted(map(luminance,colors),reverse=True)
                assert (light+.05)/(dark+.05)>=4.5
            wrappers=page.locator('[data-partial-root] .adm-table-wrap')
            assert wrappers.count()>0
            assert wrappers.first.get_attribute('tabindex')=='0'
            # Long text remains visible in DOM and wraps instead of disappearing.
            for item in page.locator('[data-partial-root] .adm-name').all():
                assert item.evaluate('(e)=>getComputedStyle(e).whiteSpace')=='normal'
            flags['empty']=True;page.reload();page.locator('[data-partial-root="'+tab+'"]').wait_for()
            assert page.locator('.adm-panel.active .adm-empty').count()>0
            page.screenshot(path=str(OUT/f'{tab}-empty-{mode}.png'),full_page=True)
            flags['empty']=False
        flags['hold']=True
        page.goto('http://fixture.invalid/admin#orders')
        page.get_by_role('status').wait_for()
        assert page.locator('[data-partial-container=orders]').get_attribute('aria-busy')=='true'
        page.screenshot(path=str(OUT/f'orders-loading-{mode}.png'),full_page=True)
        flags['hold']=False
        for r in held:
            u=urlparse(r.request.url);r.fulfill(content_type='text/html',body=render('orders',parse_qs(u.query)))
        held.clear();page.locator('#orders-q').wait_for()
        assert page.locator('[data-partial-container=orders]').get_attribute('aria-busy')=='false'
        # Filters, pagination and URL privacy through the real navigation JS.
        page.goto('http://fixture.invalid/admin#orders');page.locator('#orders-q').wait_for()
        page.locator('#orders-q').fill('inexistente');page.get_by_role('button',name='Filtrar',exact=True).click()
        page.get_by_text('Nenhum pedido para os filtros aplicados.').wait_for()
        assert 'inexistente' not in page.url
        page.screenshot(path=str(OUT/f'orders-no-results-{mode}.png'),full_page=True)
        page.locator('#orders-q').fill('PM261009TEST01');page.locator('#orders-status').select_option('paid')
        flags['fail']=True;page.get_by_role('button',name='Filtrar',exact=True).click()
        page.get_by_role('button',name='Tentar novamente').wait_for()
        failed=requests[-1];page.screenshot(path=str(OUT/f'orders-error-{mode}.png'),full_page=True)
        page.get_by_role('button',name='Tentar novamente').click();page.locator('#orders-q').wait_for()
        assert requests[-1]==failed
        assert page.locator('#orders-q').input_value()=='PM261009TEST01'
        page.get_by_role('link',name='Próxima',exact=True).click();page.get_by_text('Página 2',exact=True).wait_for()
        assert 'page=2' in page.url and 'q=' not in page.url
        page.locator('.adm-sidebar [data-panel="manual-review"]' if mode=='desktop' else '.adm-mobile-nav [data-panel="manual-review"]').click()
        page.locator('[data-partial-root="manual-review"]').wait_for()
        page.go_back();page.get_by_text('Página 2',exact=True).wait_for()
        page.go_forward();page.locator('.adm-panel.active [data-partial-root="manual-review"]').wait_for()
        # An aborted, late filter cannot overwrite the latest result.
        page.goto('http://fixture.invalid/admin#orders');page.locator('#orders-q').wait_for()
        previous_height=page.locator('[data-partial-container=orders]').bounding_box()['height']
        flags['hold']=True;page.locator('#orders-q').fill('antiga')
        page.get_by_role('button',name='Filtrar',exact=True).click()
        page.get_by_role('status').wait_for()
        assert page.locator('[data-partial-container=orders]').bounding_box()['height']>=previous_height
        page.wait_for_timeout(50)
        flags['hold']=False
        page.evaluate("fetchInto(document.querySelector('[data-partial-container=orders]'), '/admin/painel/orders?q=atual&page=2')")
        page.locator('#orders-q').wait_for();assert page.locator('#orders-q').input_value()=='atual'
        for r in held:
            try:r.fulfill(content_type='text/html',body=render('orders',{'q':['antiga']}))
            except Exception:pass # aborted route is expected
        held.clear();assert page.locator('#orders-q').input_value()=='atual'
        page.locator('.adm-panel.active .adm-table-wrap').first.focus()
        assert page.evaluate("document.activeElement.getAttribute('role')")=='region'
        # Lazy opening overview does not request any operation fragment.
        before=len(requests);page.goto('http://fixture.invalid/admin#overview');assert len(requests)==before
        assert not errors,errors
        results.append({'viewport':mode,'tabs':4,'body_overflow':False,'empty_states':4,'filters_pagination_retry_history_lazy':'passed','js_errors':errors})
        browser.close()
(OUT/'validation.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
print(json.dumps(results,ensure_ascii=False))
