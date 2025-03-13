#!/bin/bash

read -p "Enter your commit message: " commit

if [[ -z "$commit" ]]; then
	echo "Error: Commit message cannot be empty"
	exit 1
fi

git add .

git commit -m "$commit"

branch=$(git rev-parse --abbrev-ref HEAD)

git push -u origin "$branch"

echo "Changes pushed to $branch"
