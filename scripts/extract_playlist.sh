#!/bin/bash

set -e

_yml_escape(){
    printf "%s" "$1" | base64 -w0 | yq '@base64d'
}

pl=$(youtube-dl -s "$1" 2> /dev/null | head -2 | tail -1 | cut -d' ' -f4-)
cat << EOF
title: $(_yml_escape "$pl")
description:
sections:
  - title: $(_yml_escape "$pl")
    description:
    lectures:
EOF

_lecture(){
#   echo "# $1 $2"
    cat << EOF
      - title: $(_yml_escape "$2")
        description:
        type: youtube
        id: $(_yml_escape "$1")
EOF
}

while read line; do
    if [[ -z "$t" ]]; then
        t="$line"
    else
        _lecture "$line" "$t"
        unset t
    fi
done < <(youtube-dl --get-id --get-title "$1")
