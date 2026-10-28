#!/usr/bin/env bash
# Deploy the runtime to ECS Fargate (plan P5-04; docs/runbooks/deploy.md).
#
#   AWS_PROFILE=tripleq-development bash deploy/aws/deploy.sh
#
# Steps: check the account; build the image and push it to ECR; create or update the Secrets
# Manager entries; deploy the CloudFormation stack (deploy/aws/runtime.yaml); print its URL.
# Secret values come from the git-ignored .env (ZONIC_GATEWAY_API_KEY, ZONIC_MCP_AUTH_TOKEN,
# ZONIC_OPENAI_API_KEY) and
# go to Secrets Manager through temporary files: they are never printed or put on a command line.
# The checkpoint AES key is generated once in Secrets Manager and never replaced here (see
# docs/runbooks/rotate-keys.md).
#
# Overridable: STAGE (dev), ACCOUNT (001634074733), AWS_REGION (us-east-1), DESIRED_COUNT (1),
# IMAGE_SUFFIX (appended to the image tag), DOCKERFILE (Dockerfile), RESOURCES_DIR (/app/resources),
# STACK (zonic-agent-runtime-$STAGE), OIDC_POOL_ID, OIDC_AUDIENCE.
set -euo pipefail
shopt -s inherit_errexit  # a failure inside $(...) stops the script too
export MSYS_NO_PATHCONV=1  # Git Bash: leave /names alone

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
STAGE="${STAGE:-dev}"
ACCOUNT="${ACCOUNT:-001634074733}"
export AWS_REGION="${AWS_REGION:-us-east-1}"
STACK="${STACK:-zonic-agent-runtime-$STAGE}"
REPOSITORY="zonic-agent-runtime"
DESIRED_COUNT="${DESIRED_COUNT:-1}"
DOCKERFILE="${DOCKERFILE:-Dockerfile}"
RESOURCES_DIR="${RESOURCES_DIR:-/app/resources}"
OIDC_POOL_ID="${OIDC_POOL_ID:-us-east-1_lDBxGGZYh}"
# The builder UI's client and the verification client (docs/runbooks/test-client.md)
OIDC_AUDIENCE="${OIDC_AUDIENCE:-5ecn0u0cv8gm1qfe2d1kfm53jg,4egvsdjnvqfd437lto6v7g2f6p}"
SECRET_PREFIX="zonic-agent-runtime/$STAGE"

log() { printf '==> %s\n' "$*" >&2; }
fail() { printf 'error: %s\n' "$*" >&2; exit 1; }
native() { cygpath -m "$1" 2>/dev/null || printf '%s' "$1"; }
env_value() {  # the value of $1 in .env, or empty
  sed -n "s/^$1=//p" "$ROOT/.env" 2>/dev/null | tail -1 | tr -d '\r'
}

found="$(aws sts get-caller-identity --query Account --output text)"
[ "$found" = "$ACCOUNT" ] || fail "the credentials are for account $found, not $ACCOUNT"
log "account $ACCOUNT, region $AWS_REGION, stack $STACK"

# --- image ---------------------------------------------------------------------------------
registry="$ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com"
aws ecr describe-repositories --repository-names "$REPOSITORY" >/dev/null 2>&1 ||
  aws ecr create-repository --repository-name "$REPOSITORY" --image-scanning-configuration scanOnPush=true \
    --image-tag-mutability IMMUTABLE >/dev/null
repo="$(native "$ROOT")"  # Windows git and docker need the native path
tag="$(git -C "$repo" rev-parse --short HEAD)"
git -C "$repo" diff --quiet HEAD -- 2>/dev/null || tag="$tag-dirty-$(date -u +%Y%m%d%H%M%S)"
tag="$tag${IMAGE_SUFFIX:-}"
image="$registry/$REPOSITORY:$tag"
log "image $image"
aws ecr get-login-password | docker login --username AWS --password-stdin "$registry" >/dev/null
docker build --platform linux/amd64 -f "$repo/$DOCKERFILE" -t "$image" "$repo"
docker push "$image" >/dev/null

# --- secrets -------------------------------------------------------------------------------
put_secret() {  # name, value: create or update; prints the ARN
  local name="$SECRET_PREFIX/$1" file
  file="$(mktemp)"
  trap 'rm -f "$file"' RETURN
  printf '%s' "$2" > "$file"
  if aws secretsmanager describe-secret --secret-id "$name" >/dev/null 2>&1; then
    aws secretsmanager put-secret-value --secret-id "$name" --secret-string "file://$(native "$file")" >/dev/null
  else
    aws secretsmanager create-secret --name "$name" --secret-string "file://$(native "$file")" >/dev/null
  fi
  aws secretsmanager describe-secret --secret-id "$name" --query ARN --output text
}
gateway_key="$(env_value ZONIC_GATEWAY_API_KEY)"
mcp_token="$(env_value ZONIC_MCP_AUTH_TOKEN)"
openai_key="$(env_value ZONIC_OPENAI_API_KEY)"
[ -n "$gateway_key" ] || fail "ZONIC_GATEWAY_API_KEY is not set in .env"
[ -n "$mcp_token" ] || fail "ZONIC_MCP_AUTH_TOKEN is not set in .env"
[ -n "$openai_key" ] || fail "ZONIC_OPENAI_API_KEY is not set in .env"
gateway_arn="$(put_secret gateway-api-key "$gateway_key")"
mcp_arn="$(put_secret mcp-auth-token "$mcp_token")"
openai_arn="$(put_secret openai-api-key "$openai_key")"
aes_name="$SECRET_PREFIX/checkpoint-aes-key"
if ! aws secretsmanager describe-secret --secret-id "$aes_name" >/dev/null 2>&1; then
  log "generating the checkpoint AES key"
  aes_key="$(openssl rand -hex 32)"
  [ "${#aes_key}" -eq 64 ] || fail "could not generate the checkpoint AES key"
  aes_arn="$(put_secret checkpoint-aes-key "$aes_key")"
  unset aes_key
else
  aes_arn="$(aws secretsmanager describe-secret --secret-id "$aes_name" --query ARN --output text)"
fi
log "secrets under $SECRET_PREFIX/"

# --- stack ---------------------------------------------------------------------------------
gateway_url="$(env_value ZONIC_GATEWAY_BASE_URL)"
mcp_url="$(env_value ZONIC_MCP_BASE_URL)"
[ -n "$gateway_url" ] || fail "ZONIC_GATEWAY_BASE_URL is not set in .env"
log "deploying $STACK"
aws cloudformation deploy --stack-name "$STACK" --template-file "$(native "$ROOT/deploy/aws/runtime.yaml")" \
  --capabilities CAPABILITY_IAM --no-fail-on-empty-changeset \
  --tags project=zonic-agent-runtime stage="$STAGE" \
  --parameter-overrides \
    Stage="$STAGE" ImageUri="$image" DesiredCount="$DESIRED_COUNT" ResourcesDir="$RESOURCES_DIR" \
    GatewayBaseUrl="$gateway_url" McpBaseUrl="$mcp_url" \
    OidcIssuer="https://cognito-idp.$AWS_REGION.amazonaws.com/$OIDC_POOL_ID" OidcAudience="$OIDC_AUDIENCE" \
    GatewayApiKeySecretArn="$gateway_arn" McpAuthTokenSecretArn="$mcp_arn" CheckpointAesKeySecretArn="$aes_arn" OpenAiApiKeySecretArn="$openai_arn"
output() { aws cloudformation describe-stacks --stack-name "$STACK" --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
aws ecs wait services-stable --cluster "$(output Cluster)" --services runtime
url="$(output Url)"
log "ready: $url"
curl -fsS "$url/readyz" && echo
