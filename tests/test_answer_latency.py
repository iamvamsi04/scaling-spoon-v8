"""Provider-call regression: no speculative answer or formatting-retry inference."""
import asyncio
import json
import logging
import pytest
from autogen_core import FunctionCall
from autogen_core.models import CreateResult, RequestUsage
from agent import billing_agent as module
from config import settings


def result(content):
    return CreateResult(content=content,finish_reason='stop' if isinstance(content,str) else 'function_calls',
                        usage=RequestUsage(prompt_tokens=10,completion_tokens=5),cached=False)


@pytest.mark.parametrize('mode,style,extra_read',[
    ('customer','plain',False), ('customer','envelope',False),
    ('customer','fenced',False), ('admin','plain',False), ('customer','envelope',True)])
def test_one_provider_call_per_required_step(monkeypatch,caplog,mode,style,extra_read):
    caplog.set_level(logging.INFO, logger='uvicorn.error.billing_performance')
    for field,value in {'AZURE_OPENAI_ENDPOINT':'https://example.openai.azure.com',
        'AZURE_OPENAI_API_KEY':'test-only','AZURE_OPENAI_DEPLOYMENT':'test',
        'AZURE_OPENAI_MODEL':'gpt-5.4','AZURE_OPENAI_API_VERSION':'2024-10-21'}.items():
        monkeypatch.setattr(settings,field,value)
    calls=[]
    args={'query':'SELECT item_code,name,availablequantity FROM items ORDER BY item_code'}
    if mode=='admin':
        args['chart']={'type':'bar','title':'Stock','x':'name','y':['availablequantity']}
    envelope=json.dumps({'reply':'Here are the products.', 'shopping_update':{
        'displayed_product_ids':['ITEM001','ITEM002','ITEM003']}})
    final='Here are the products.' if style=='plain' else envelope if style=='envelope' else '```json\n'+envelope+'\n```'
    script=[result([FunctionCall(id='read1',name='execute_sql_query',arguments=json.dumps(args))])]
    if extra_read:
        script.append(result([FunctionCall(id='read2',name='execute_sql_query',arguments=json.dumps({
            'query':'SELECT COUNT(*) AS products FROM items'}))]))
    script.append(result(final))
    async def create(self,messages,**kwargs):
        calls.append(messages)
        assert len(calls)<=len(script), 'Unexpected duplicate model call'
        return script[len(calls)-1]
    monkeypatch.setattr(module.AzureOpenAIChatCompletionClient,'create',create)
    async def run():
        engine=module.BillingAgent()
        await engine.start()
        try:
            response=await engine.chat('list item codes, names and stock quantities'+(' as a stock chart' if mode=='admin' else ''),[],None,mode=mode)
            assert len(calls)==3 if extra_read else len(calls)==2
            assert engine._workbench.model_calls == len(calls)
            assert engine._workbench.input_tokens == len(calls) * 10
            assert engine._workbench.output_tokens == len(calls) * 5
            assert engine._workbench.tool_calls==2 if extra_read else engine._workbench.tool_calls==1
            assert all(m[0].content==calls[0][0].content for m in calls)
            assert response['response']=='Here are the products.'
            if mode=='admin':
                assert len(response['charts'])==1
            elif style!='plain':
                assert response['shopping_state']['displayed_product_ids']==['ITEM001','ITEM002','ITEM003']
        finally:
            await engine.close()
    asyncio.run(run())
    assert f'model_calls={len(calls)} input_tokens={len(calls)*10} output_tokens={len(calls)*5}' in caplog.text
