#!/bin/bash

source "$(dirname "$0")/../.env"

curl "https://graph.facebook.com/debug_token\
?input_token=$TOKEN\
&access_token=$APP_ID|$APP_SECRET"

