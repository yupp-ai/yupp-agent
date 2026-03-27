from fastapi import FastAPI

# a sub-app for streaming endpoints only, this doesn't have GZip middleware
streaming_subapp = FastAPI()
