#!/bin/bash

set -e

pl=$(youtube-dl -j --playlist-end 1 "$1" | jq -c '[.playlist_title,.playlist_uploader]')
cat << EOF
# $1
title: $(jq .[0] <<< "$pl")
description:
category:
language:
image:
author: $(jq .[1] <<< "$pl")
price:
learning_goals: []
requirements: []
last_update: $(date +%s)
sections:
  - id: section
    title: $(jq .[0] <<< "$pl")
    description:
    lectures:
EOF

while read line; do
  data=$(jq -Rc '@base64d|fromjson|[.id,.title,.duration]' <<< "$line")
  id=$(jq .[0] <<< "$data")
  title=$(jq .[1] <<< "$data")
  duration=$(jq .[2] <<< "$data")
  cat << EOF
      - id: $id
        title: $title
        description:
        type: youtube
        video_id: $id
        duration: $duration
EOF
done < <(youtube-dl -j "$1" | jq -r 'tojson|@base64')
