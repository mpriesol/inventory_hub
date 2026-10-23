/* Exact-code lookup against synthetic Hub responses; no external reads or writes. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'https://hub.example.test/product-trace' });
global.window = dom.window; global.document = dom.window.document; global.HTMLElement = dom.window.HTMLElement;
Object.defineProperty(global, 'navigator', { value: dom.window.navigator, configurable: true });
global.IS_REACT_ACT_ENVIRONMENT = true;
for (const extension of ['.ts', '.tsx']) require.extensions[extension] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8').replace(/import\.meta\.env/g, '{}'), { compilerOptions: { jsx: ts.JsxEmit.React, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, esModuleInterop: true } }).outputText, filename);
require.extensions['.css'] = () => {};
const React = require('react'); const { act } = React; const { createRoot } = require('react-dom/client');
const { createMemoryRouter, RouterProvider } = require('react-router-dom');
const i18n = require('../src/i18n/index.ts').default;
const { ProductTracePage } = require('../src/pages/ProductTracePage.tsx');
const { unlockHub } = require('../src/api/access.ts');
const root = createRoot(document.getElementById('root'));
const tick = () => new Promise(resolve => setTimeout(resolve, 5));
const find = id => document.querySelector(`[data-testid="${id}"]`);
const t = (key, values) => i18n.t(`productTrace.${key}`, values);
async function click(id) { await act(async () => { assert(find(id),id); find(id).click(); await tick(); }); }
async function input(value) { await act(async () => { const node=find('trace-code'); Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype,'value').set.call(node,value); node.dispatchEvent(new dom.window.Event('input',{bubbles:true})); await tick(); }); }
async function search(value) { await input(value); await act(async () => { find('trace-code').closest('form').dispatchEvent(new dom.window.Event('submit',{bubbles:true,cancelable:true})); await tick(); }); }
const reply = (value, status = 200) => ({ ok: status<400, status, json:async()=>value });
const product = {id:4,sku:'TP-000123',name:'Synthetic leading-zero product',brand:'Topeak',item_type:'stock',stock_unit:'pcs',eans:['0001234500000'],card_url:'/stock?q=TP-000123',image_url:'https://images.example.test/product.jpg',shops:[{code:'biketrek',name:'BIKETREK',url:'https://www.biketrek.sk/p/synthetic',admin_url:'https://admin.example.test/product/4'}]};
const invoice = {invoice_id:91,line_id:901,product_name:'Invoice line product',direction:'purchase',invoice_number:'SUP-2026-01',invoice_date:'2026-10-02',due_date:'2026-10-16',supplier_name:'Synthetic supplier',shop_code:null,currency:'USD',quantity:'0',unit:'pcs',unit_price:'0',total_price:'0',matched_by:'barcode'};
const movement = {id:8,product_id:4,sku:product.sku,product_name:product.name,warehouse_code:'main',warehouse_name:'Main',movement_type:'SALE_OUT',quantity:'-2',balance_before:'5',balance_after:'3',unit_cost:null,total_cost:null,cost_basis:'unknown',reference_type:'order',reference_id:'order-1',reference_label:'BT-1',document:null,tracking_scope:'current',created_at:'2026-10-10T10:30:00Z'};
function result(params) {
 const code=params.get('code')||'', invoicePage=Number(params.get('invoice_page')||1), movementPage=Number(params.get('movement_page')||1);
 const data={code,matches:[product],product:{...product,name:code==='NEW-CODE'?'Latest product':product.name},stocks:[{warehouse_code:'main',warehouse_name:'Main',on_hand:'3',reserved:'1',quarantined:'0',available:'2',confirmed:true},{warehouse_code:'old',warehouse_name:'Old unconfirmed',on_hand:'987654',reserved:'0',quarantined:'0',available:'987654',confirmed:false}],prices:[{source:'shop',label:'BIKETREK',price:'0',currency:'EUR',vat_included:true,observed_at:null},{source:'invoice',label:'Invoice purchase',price:'10.1250',currency:'USD',vat_included:false,observed_at:'2026-10-02',source_invoice_unit:'pack'},{source:'unknown',label:'Unknown source price',price:null,currency:null,vat_included:null,observed_at:null}],invoices:{items:invoicePage===1?[invoice,{...invoice,invoice_id:92,line_id:902,invoice_number:'UNKNOWN-CURRENCY',direction:'sale',currency:null,quantity:null,unit_price:null,total_price:null}]:[{...invoice,invoice_id:93,line_id:903,invoice_number:'PAGE-TWO-INVOICE'}],total:51,page:invoicePage,page_size:50},sales:{sold_quantity:'8',returned_quantity:'2',net_quantity:'6',unit:'pcs',source:'hub_ledger',since:'2026-09-01',monthly:[{month:'2026-09',sold:'5',returned:'1',net:'4'},{month:'2026-10',sold:'3',returned:'1',net:'2'}]},movements:{items:[{...movement,id:movementPage===1?8:9,reference_label:movementPage===1?'BT-1':'PAGE-TWO-MOVEMENT'}],total:51,page:movementPage,page_size:50},warnings:['sales_hub_ledger_only','stock_not_confirmed']};
 data.sales_documents={source:'shop_invoice_archive',by_unit:[{unit:'pack',sold_quantity:'12',returned_quantity:'2',net_quantity:'10'},{unit:'m',sold_quantity:'1.25',returned_quantity:'0',net_quantity:'1.25'}],monthly:[{month:'2026-10',unit:'pack',sold:'12',returned:'2',net:'10'},{month:'2026-10',unit:'m',sold:'1.25',returned:'0',net:'1.25'}]};
 if(code==='AMBIGUOUS'&&!params.has('product_id')) Object.assign(data,{product:null,matches:[product,{...product,id:5,sku:'OTHER-000123',name:'Other exact match'}],warnings:['ambiguous_code'],stocks:[],prices:[],sales:null,matches_truncated:true});
 if(code==='INVOICE-ONLY') Object.assign(data,{product:null,matches:[],stocks:[],prices:[],sales:null,warnings:['product_not_found','invoice_index_incomplete']});
 if(code==='FEED-ONLY') Object.assign(data,{product:null,matches:[],stocks:[],prices:[],sales:null,warnings:['product_not_found'],feed_matches:{items:[{id:8,supplier_code:'cyklozitny',supplier_name:'Cykložitný',supplier_sku:'000-SUP',manufacturer_sku:'M-1',name:'Feed-only part',brand:'Fixture',eans:['0001234500000'],image_url:'https://images.example.test/feed.jpg',purchase_price:'7.20',purchase_currency:'CZK',recommended_price:null,catalog_currency:null,stock_qty:'3',supplier_availability_text:null,is_active:true,last_seen_at:'2026-10-22T10:00:00Z',feed_code:'products',hub_product_id:null,hub_sku:null,catalog_url:'/suppliers/cyklozitny/catalog?code=000-SUP&feed=products'}],truncated:false}});
 if(code==='SERVICE') Object.assign(data,{product:{...product,item_type:'service'},stocks:[]});
 return data;
}
const calls=[];let deferred;
global.fetch=async(path, options={})=>{
 const params=new URL(path,dom.window.location).searchParams;calls.push({path,params,options});
 assert.equal(new URL(path,dom.window.location).pathname,'/api/product-trace');assert.equal(options.method,'GET');assert(!options.body);
 assert.equal(options.cache,'no-store');assert(options.headers.Authorization.startsWith('Bearer synthetic-'));
 if(params.get('code')==='FAIL')return reply({detail:{code:'product_trace_selection_invalid'}},400);
 if(params.get('code')==='SLOW')return new Promise(resolve=>{deferred=()=>resolve(reply(result(params)));});
 return reply(result(params));
};
let router;
async function render(entry='/product-trace') {router=createMemoryRouter([{path:'*',element:React.createElement(ProductTracePage)}],{initialEntries:[entry]});await act(async()=>{root.render(React.createElement(RouterProvider,{router}));await tick();});}
(async()=>{
 unlockHub('');await render('/product-trace?code=0001234500000');assert.equal(calls.length,0,'Locked views do not read protected records');
 await act(async()=>{unlockHub('synthetic-trace-token');await tick();});
 assert.equal(calls.at(-1).params.get('code'),'0001234500000','EAN leading zeros are preserved in deep links');
 assert(document.querySelector('img[src="https://images.example.test/product.jpg"]'));
 assert(document.querySelector('a[href="https://www.biketrek.sk/p/synthetic"]'));
 assert(document.querySelector('a[href="https://admin.example.test/product/4"]'));
 assert(document.body.textContent.includes(t('salesScope')),'Sales are explicitly limited to the Hub ledger');
 const archiveSales=find('trace-document-sales');assert(archiveSales.textContent.includes(t('salesDocumentsScope')),'Invoice sales are explicitly separate from stock issues');assert(archiveSales.textContent.includes('1,25'));assert(archiveSales.textContent.includes(i18n.t('productEditor.units.pack')));assert(archiveSales.textContent.includes(i18n.t('productEditor.units.m')));assert.equal(archiveSales.querySelector('tbody').children.length,2,'Invoice units are shown in separate rows, never converted or summed');
 assert(!document.body.textContent.includes('987654'),'Unconfirmed historical balance is never presented as current stock');
 assert(find('trace-price-0').textContent.includes('0,00 EUR'),'A known zero price stays zero');
 assert(find('trace-price-1').textContent.includes('10,125 USD'),'Currencies and four-decimal unit costs stay distinct');
 assert.equal(find('trace-price-2').textContent,t('unknown'),'Missing prices never become zero');
 assert(document.querySelector('a[href="/invoices/91"]'),'Invoice links use the canonical document ID');
 const zeroInvoice=document.querySelector('a[href="/invoices/91"]').closest('tr');assert(zeroInvoice.textContent.includes('0,00 USD'));assert(!zeroInvoice.textContent.includes('EUR'));assert(zeroInvoice.textContent.includes(t('inbound')),'Purchase invoices are labelled received');
 const unknownInvoice=document.querySelector('a[href="/invoices/92"]').closest('tr');assert(unknownInvoice.textContent.includes(t('unknown')));assert(!unknownInvoice.textContent.includes('0,00'));assert(unknownInvoice.textContent.includes(t('outbound')),'Sale invoices are labelled issued using the API direction value');
 await click('trace-invoice_page-next');assert(document.body.textContent.includes('PAGE-TWO-INVOICE'));assert.equal(calls.at(-1).params.get('movement_page'),'1');
 await click('trace-movement_page-next');assert(document.body.textContent.includes('PAGE-TWO-MOVEMENT'));assert.equal(calls.at(-1).params.get('invoice_page'),'2');
 const beforeTyping=calls.length;await input('AMBIGUOUS');assert.equal(calls.length,beforeTyping,'Scanner input does not query until submitted');await search('AMBIGUOUS');
 assert(find('trace-match-4')&&find('trace-match-5'),'An ambiguous EAN offers explicit card choices');assert(!document.querySelector('#trace-product-name'));assert(!find('trace-document-sales'),'Ambiguous card owners never share a sales aggregate');assert(document.body.textContent.includes(t('matchesTruncated')));
 await click('trace-match-4');assert.equal(calls.at(-1).params.get('product_id'),'4');assert.equal(calls.at(-1).params.get('invoice_page'),'1');assert(document.querySelector('#trace-product-name'));
 await search('INVOICE-ONLY');assert(!document.querySelector('#trace-product-name'));assert(document.querySelector('a[href="/invoices/91"]'),'Invoice-code lookup works without an existing Hub card');assert(document.body.textContent.includes(t('warnings.invoice_index_incomplete')));
 await search('FEED-ONLY'); assert(!document.querySelector('#trace-product-name'));
 assert(document.body.textContent.includes('Feed-only part'));assert(document.body.textContent.includes('7,20 CZK'));
 assert(document.querySelector('a[href="/suppliers/cyklozitny/catalog?code=000-SUP&feed=products"]'));
 assert(!document.querySelector('a[href="/product-trace?product_id=8"]'),'Feed row IDs never fabricate Hub card links');
 await search('SERVICE'); assert(!document.body.textContent.includes('Feed-only part'),'A new lookup clears previous feed results');assert(document.body.textContent.includes(t('serviceSales')));assert(document.body.textContent.includes(i18n.t('productEditor.serviceHelp')));
 await search('SLOW');assert(deferred);const slow=calls.at(-1);await search('NEW-CODE');assert(slow.options.signal.aborted,'A new scan aborts the old request');
 await act(async()=>{deferred();await tick();});assert.equal(document.querySelector('#trace-product-name').textContent,'Latest product','A late response cannot replace the latest lookup');
 await search('FAIL');assert(document.body.textContent.includes(t('errors.product_trace_selection_invalid')));assert(!document.querySelector('#trace-product-name'),'Old results disappear on failed selection');
 await act(async()=>{await router.navigate('/product-trace?product_id=4');await tick();});assert.equal(calls.at(-1).params.get('product_id'),'4');assert(!calls.at(-1).params.has('code'),'Direct card lookup does not fabricate a barcode');
 await act(async()=>{unlockHub('');await tick();});assert(!document.querySelector('#trace-product-name'),'Losing access clears private results');
 assert(calls.every(call=>call.options.method==='GET'),'Trace never changes stock, products, invoices or shops');
 await act(async()=>root.unmount());console.log('Product trace UI passed: exact EAN, explicit selection, zero/null/currency, invoice-only results, scoped sales, independent pagination, stale-response and access guards.');
})().catch(error=>{console.error(error);process.exitCode=1;});
