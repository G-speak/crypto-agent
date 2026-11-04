"""Text-only initial screening. No trading, credentials loading or messaging."""
import json
import time
MODELS=('gpt-5.6-luna','gpt-4o-mini','gpt-4.1')

def decode(text):
    if not isinstance(text,str):raise ValueError('not_text')
    text=text.strip()
    if text.startswith('```') and text.endswith('```'):
        text=text.split('\n',1)[1].rsplit('```',1)[0].strip()
    value=json.loads(text)
    if not isinstance(value,dict) or value.get('action') not in ('BUY','SELL','HOLD'):raise ValueError('invalid_action')
    if not isinstance(value.get('reason'),str) or not value['reason'].strip() or len(value['reason'])>500:raise ValueError('invalid_reason')
    return {'action':value['action'],'reason':value['reason']}

def request_decision(prompt,system,key,model='auto',post=None,clock=time.monotonic):
    if post is None:
        import requests
        post=requests.post
    attempts=[];start=clock()
    if not key:return failure([{'error':'missing_key'}])
    for candidate in MODELS if model=='auto' else (model,):
        remaining=40-(clock()-start)
        if remaining<2:
            attempts.append({'error':'attempt_budget_exhausted'});break
        try:
            response=post('https://api.openlux.ai/v1/chat/completions',
                headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'},
                json={'model':candidate,'messages':[{'role':'system','content':system},{'role':'user','content':prompt}],
                      'temperature':0.1,'max_tokens':1200},
                timeout=(min(3,remaining/2),min(10,remaining/2)),allow_redirects=False)
            status=response.status_code
            if status!=200:
                attempts.append({'model':candidate,'http_status':status})
                if status in (401,403):break
                continue
            data=response.json()
            choice=data['choices'][0]
            if choice.get('finish_reason')=='length':raise ValueError('truncated_output')
            result=decode(choice['message']['content'])
            result.update(ai_status='ok',ai_model=candidate,ai_attempts=attempts)
            return result
        except Exception as exc:
            # Error bodies/URLs can include credentials. Keep only safe categories.
            attempts.append({'model':candidate,'error':type(exc).__name__})
    return failure(attempts)

def failure(attempts):
    return {'action':'HOLD','reason':'所有模型调用失败（本轮初筛未取得有效结构化结论）',
            'ai_status':'unavailable','ai_attempts':attempts,
            'ai_error':'结构化初筛未完成；本轮尝试结果：'+json.dumps(attempts,ensure_ascii=False)}
