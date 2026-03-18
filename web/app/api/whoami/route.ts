import { NextRequest, NextResponse } from "next/server";
import { createClient } from "@supabase/supabase-js";

const supabaseUrl = process.env.SUPABASE_URL;
const serviceRoleKey = process.env.SUPABASE_SERVICE_ROLE_KEY;

if (!supabaseUrl || !serviceRoleKey) {
  throw new Error(
    "Missing Supabase environment variables. Please set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY.",
  );
}

const supabaseServerClient = createClient(supabaseUrl, serviceRoleKey);

export async function GET(request: NextRequest) {
  const authHeader = request.headers.get("authorization") ?? "";

  if (!authHeader.toLowerCase().startsWith("bearer ")) {
    return NextResponse.json(
      { error: "Missing or invalid Authorization header" },
      { status: 401 },
    );
  }

  const accessToken = authHeader.slice("bearer ".length).trim();

  if (!accessToken) {
    return NextResponse.json(
      { error: "Missing access token" },
      { status: 401 },
    );
  }

  const {
    data: { user },
    error,
  } = await supabaseServerClient.auth.getUser(accessToken);

  if (error || !user) {
    return NextResponse.json(
      { error: "Invalid or expired token" },
      { status: 401 },
    );
  }

  return NextResponse.json(
    {
      user_id: user.id,
      email: user.email,
    },
    { status: 200 },
  );
}

