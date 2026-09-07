"""Execute production JS helpers with controlled network failures and delays."""

import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest


STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
JS = (STATIC / "app.js").read_text(encoding="utf-8")


def run_js(names, scenario):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required to execute frontend regression tests")
    functions = []
    for name in names:
        match = re.search(r"(?:async )?function " + name + r"\([^\n]*[\s\S]*?\n}", JS)
        assert match, name
        functions.append(match.group())
    script = "const assert = require('node:assert/strict');\n" + "\n".join(functions)
    script += "\n(async () => {\n" + scenario + "\n})().catch(e => { console.error(e); process.exit(1); });"
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("refresh_ok", [True, False])
def test_double_submit_writes_once_and_distinguishes_refresh_failure(refresh_ok):
    run_js(["submitData"], """
      let resolve, calls = 0, closed = 0;
      const messages = [], button = {disabled:false};
      const attrs = new Map();
      const form = {querySelectorAll:()=>[button], setAttribute:(k,v)=>attrs.set(k,v), removeAttribute:k=>attrs.delete(k)};
      global.document = {querySelector:()=>form};
      global.api = async () => { calls++; return new Promise(r => { resolve = r; }); };
      global.closeModal = () => { closed++; };
      global.toast = (text,type) => messages.push({text,type});
      global.loadAll = async () => REFRESH_OK;
      const first = submitData('/withdrawals', {}, 'Salvo');
      await submitData('/withdrawals', {}, 'Salvo');
      assert.equal(calls,1);
      assert.equal(button.disabled,true);
      assert.equal(attrs.get('aria-busy'),'true');
      resolve({}); await first;
      assert.equal(button.disabled,false);
      assert.equal(attrs.has('aria-busy'),false);
      assert.equal(submitData.pending,false);
      assert.equal(closed,1);
      assert.equal(messages.at(-1).type, REFRESH_OK ? 'success' : 'error');
      if (!REFRESH_OK) assert.match(messages.at(-1).text,/Operação salva/);
    """.replace("REFRESH_OK", json.dumps(refresh_ok)))


def test_failed_submit_keeps_form_and_releases_lock():
    run_js(["submitData"], """
      const button={disabled:false}; let closed=false;
      global.document={querySelector:()=>({querySelectorAll:()=>[button],setAttribute:()=>{},removeAttribute:()=>{}})};
      global.api=async()=>{throw new Error('Sem saldo');};
      global.closeModal=()=>{closed=true;}; global.toast=()=>{};
      await submitData('/withdrawals',{},'Salvo');
      assert.equal(closed,false); assert.equal(button.disabled,false); assert.equal(submitData.pending,false);
    """)


def test_connection_failure_is_explicit_about_unknown_write_result():
    run_js(["api", "describeError"], """
      global.window={}; Object.defineProperty(global,'navigator',{value:{onLine:true},configurable:true});
      global.fetch=async()=>{throw new TypeError('Failed to fetch');};
      await assert.rejects(api('/inventory'),/Verifique sua internet/);
      await assert.rejects(api('/withdrawals',{method:'POST',body:'{}'}),/Confira o histórico antes de repetir/);
      global.fetch=async()=>({ok:true,status:204});
      assert.equal(await api('/item',{method:'DELETE'}),null);
    """)


def test_clearing_withdrawal_quantity_revokes_negative_stock_override():
    run_js(["refreshWithdrawalPreview"], """
      const fields={'withdrawal-preview':{},'withdrawal-override':{checked:true},'withdrawal-override-field':{hidden:false}};
      global.document={getElementById:id=>fields[id]}; global.readWithdrawalLines=()=>[];
      refreshWithdrawalPreview();
      assert.equal(fields['withdrawal-override'].checked,false);
      assert.equal(fields['withdrawal-override-field'].hidden,true);
    """)


def test_local_date_does_not_roll_over_at_utc_midnight():
    # Set TZ before constructing dates; Node supports this on Windows as well.
    run_js(["localDateTimeValue", "dateInputValue"], """
      process.env.TZ='America/Sao_Paulo';
      assert.equal(dateInputValue(new Date('2026-09-08T01:30:00Z')),'2026-09-07');
    """)


def test_fractional_withdrawal_does_not_report_false_stock_shortage():
    run_js(["refreshWithdrawalPreview"], """
      const fields={'withdrawal-preview':{},'withdrawal-override':{checked:false},'withdrawal-override-field':{hidden:true}};
      global.document={getElementById:id=>fields[id]};
      global.readWithdrawalLines=()=>[{inventory_id:'item',quantity:0.1},{inventory_id:'item',quantity:0.2}];
      global.state={inventory:[{id:'item',name:'Insumo',quantity:0.3,unit:'kg',unit_cost:10}]};
      global.number=new Intl.NumberFormat('pt-BR'); global.currency=new Intl.NumberFormat('pt-BR',{style:'currency',currency:'BRL'});
      refreshWithdrawalPreview();
      assert.equal(fields['withdrawal-override-field'].hidden,true);
      assert.equal(fields['withdrawal-preview'].className,'modal-result full positive');
    """)


def test_stock_formatter_preserves_small_quantities():
    declaration = re.search(r'const number = new Intl.NumberFormat\([^\n]+', JS).group()
    run_js([], declaration + "\nassert.equal(number.format(0.001), '0,001');")


@pytest.mark.parametrize("invalid", ["-1", "0", "Infinity"])
def test_purchase_preview_does_not_calculate_invalid_quantities(invalid):
    run_js(["refreshPurchasePreview"], """
      const fields={'purchase-form':{elements:{inventory_id:{value:'item'},purchase_quantity:{value:INVALID},units_per_purchase:{value:'1'},purchase_unit_cost:{value:'5'}}},'purchase-current':{},'purchase-preview':{},'purchase-content-hint':{}};
      global.document={getElementById:id=>fields[id]};
      global.state={inventory:[{id:'item',quantity:5,unit:'kg',unit_cost:10}]};
      global.number=new Intl.NumberFormat('pt-BR'); global.currency=number;
      refreshPurchasePreview();
      assert.equal(fields['purchase-preview'].className,'modal-result full neutral');
    """.replace("INVALID", json.dumps(invalid)))


def test_production_preview_rejects_consuming_its_own_output():
    run_js(["refreshProductionPreview"], """
      const fields={'production-form':{elements:{output_inventory_id:{value:'item'},output_quantity:{value:'2'}}},'production-preview':{},'production-output-unit':{},'production-override':{checked:false},'production-override-field':{}};
      global.document={getElementById:id=>fields[id]};
      global.state={inventory:[{id:'item',quantity:5,unit:'kg',unit_cost:10}]};
      global.readProductionLines=()=>[{inventory_id:'item',quantity:1}];
      refreshProductionPreview();
      assert.match(fields['production-preview'].textContent,/diferente/);
      assert.equal(fields['production-preview'].className,'modal-result full negative');
    """)
