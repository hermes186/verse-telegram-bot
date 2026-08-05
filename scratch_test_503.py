import openai
import httpx

try:
    response = httpx.Response(503, request=httpx.Request("GET", "https://api.openai.com"))
    from openai import DefaultHttpxClient
    client = openai.OpenAI(api_key="sk-test")
    
    # raise it using make_status_error
    err = client._make_status_error(
        response=response,
        body={'error': {'message': 'Service temporarily unavailable', 'type': 'api_error'}},
        err_msg="Service temporarily unavailable"
    )
    raise err
except Exception as e:
    print(f"Type: {type(e)}")
    print(f"Is InternalServerError? {isinstance(e, openai.InternalServerError)}")
    print(f"Is APIError? {isinstance(e, openai.APIError)}")
    print(f"Is APIStatusError? {isinstance(e, openai.APIStatusError)}")
