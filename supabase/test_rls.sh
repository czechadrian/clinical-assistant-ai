#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# RLS smoke-test: verify cross-user access is blocked
#
# Prerequisites:
#   export SUPABASE_URL=https://<project>.supabase.co
#   export SUPABASE_ANON_KEY=<anon key>          # public anon key, not service role
#   export USER_A_EMAIL=a@example.com
#   export USER_A_PASS=testpassword1
#   export USER_B_EMAIL=b@example.com
#   export USER_B_PASS=testpassword2
#
# Create both users first in the Supabase dashboard → Authentication → Users
# or via: supabase auth admin create-user --email ... --password ...
# ---------------------------------------------------------------------------

set -euo pipefail

AUTH="$SUPABASE_URL/auth/v1"
REST="$SUPABASE_URL/rest/v1"

# ---------------------------------------------------------------------------
# 1. Sign in as user A, capture JWT
# ---------------------------------------------------------------------------
echo "--- Signing in as user A ---"
TOKEN_A=$(curl -s -X POST "$AUTH/token?grant_type=password" \
  -H "apikey: $SUPABASE_ANON_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"email\":\"$USER_A_EMAIL\",\"password\":\"$USER_A_PASS\"}" \
  | jq -r '.access_token')

echo "Token A: ${TOKEN_A:0:20}..."

# ---------------------------------------------------------------------------
# 2. Sign in as user B, capture JWT
# ---------------------------------------------------------------------------
echo "--- Signing in as user B ---"
TOKEN_B=$(curl -s -X POST "$AUTH/token?grant_type=password" \
  -H "apikey: $SUPABASE_ANON_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"email\":\"$USER_B_EMAIL\",\"password\":\"$USER_B_PASS\"}" \
  | jq -r '.access_token')

echo "Token B: ${TOKEN_B:0:20}..."

# ---------------------------------------------------------------------------
# 3. User A creates a conversation
# ---------------------------------------------------------------------------
echo ""
echo "--- User A: create conversation ---"
CONV=$(curl -s -X POST "$REST/conversations" \
  -H "apikey: $SUPABASE_ANON_KEY" \
  -H "Authorization: Bearer $TOKEN_A" \
  -H "Content-Type: application/json" \
  -H "Prefer: return=representation" \
  -d '{"user_id": null}' | jq '.[0]')
# Note: user_id is set via RLS auth.uid(), not the client body — the column default
# in the INSERT policy enforces auth.uid() = user_id.
CONV_ID=$(echo "$CONV" | jq -r '.id')
echo "Created conversation: $CONV_ID"

# ---------------------------------------------------------------------------
# 4. User A inserts a message into that conversation
# ---------------------------------------------------------------------------
echo ""
echo "--- User A: insert message ---"
curl -s -X POST "$REST/messages" \
  -H "apikey: $SUPABASE_ANON_KEY" \
  -H "Authorization: Bearer $TOKEN_A" \
  -H "Content-Type: application/json" \
  -H "Prefer: return=representation" \
  -d "{\"conversation_id\":\"$CONV_ID\",\"role\":\"user\",\"content\":{\"text\":\"hello\"},\"user_id\":null}" \
  | jq '.[0].id'

# ---------------------------------------------------------------------------
# 5. User B tries to read user A's conversation — EXPECT: empty array []
# ---------------------------------------------------------------------------
echo ""
echo "--- User B: read user A's conversations (EXPECT: []) ---"
RESULT=$(curl -s "$REST/conversations?id=eq.$CONV_ID" \
  -H "apikey: $SUPABASE_ANON_KEY" \
  -H "Authorization: Bearer $TOKEN_B")
echo "Result: $RESULT"
if [ "$RESULT" = "[]" ]; then
  echo "PASS: user B cannot see user A's conversation"
else
  echo "FAIL: RLS leak — user B saw: $RESULT"
  exit 1
fi

# ---------------------------------------------------------------------------
# 6. User B tries to read user A's messages — EXPECT: empty array []
# ---------------------------------------------------------------------------
echo ""
echo "--- User B: read user A's messages (EXPECT: []) ---"
RESULT=$(curl -s "$REST/messages?conversation_id=eq.$CONV_ID" \
  -H "apikey: $SUPABASE_ANON_KEY" \
  -H "Authorization: Bearer $TOKEN_B")
echo "Result: $RESULT"
if [ "$RESULT" = "[]" ]; then
  echo "PASS: user B cannot see user A's messages"
else
  echo "FAIL: RLS leak — user B saw: $RESULT"
  exit 1
fi

# ---------------------------------------------------------------------------
# 7. User B tries to INSERT a message into user A's conversation
#    EXPECT: 403 with {"code":"42501",...} (RLS violation)
# ---------------------------------------------------------------------------
echo ""
echo "--- User B: insert message into user A's conversation (EXPECT: 403/42501) ---"
RESULT=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$REST/messages" \
  -H "apikey: $SUPABASE_ANON_KEY" \
  -H "Authorization: Bearer $TOKEN_B" \
  -H "Content-Type: application/json" \
  -d "{\"conversation_id\":\"$CONV_ID\",\"role\":\"user\",\"content\":{\"text\":\"injected\"},\"user_id\":null}")
echo "HTTP status: $RESULT"
if [ "$RESULT" = "403" ] || [ "$RESULT" = "401" ]; then
  echo "PASS: insert blocked"
else
  echo "FAIL: expected 403, got $RESULT"
  exit 1
fi

# ---------------------------------------------------------------------------
# 8. User A can still read their own conversation — EXPECT: 1 row
# ---------------------------------------------------------------------------
echo ""
echo "--- User A: read own conversation (EXPECT: 1 row) ---"
RESULT=$(curl -s "$REST/conversations?id=eq.$CONV_ID" \
  -H "apikey: $SUPABASE_ANON_KEY" \
  -H "Authorization: Bearer $TOKEN_A" | jq 'length')
echo "Row count: $RESULT"
if [ "$RESULT" = "1" ]; then
  echo "PASS: user A can read their own conversation"
else
  echo "FAIL: user A got $RESULT rows"
  exit 1
fi

echo ""
echo "=== All RLS checks passed ==="
