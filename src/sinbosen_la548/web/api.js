// The studio's local JSON API. Every POST is JSON (the server refuses anything else) and answers with the whole state
// plus a top-level `result`; a non-2xx answer carries `{"error": message}`.

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

async function call(method, path, body) {
  const options = { method, headers: { Accept: "application/json" } };
  if (method === "POST") {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body ?? {});
  }
  let response;
  try {
    response = await fetch(path, options);
  } catch {
    throw new ApiError("The studio server is not reachable.", 0);
  }
  let data = null;
  try {
    data = await response.json();
  } catch {
    // Left null: reported below.
  }
  if (!response.ok) {
    throw new ApiError(data?.error ?? `Request failed (${response.status}).`, response.status);
  }
  if (data === null) {
    throw new ApiError("The server sent an unreadable answer.", response.status);
  }
  return data;
}

export const getState = () => call("GET", "/api/state");
export const post = (path, body) => call("POST", path, body);
export const getPrograms = () => call("GET", "/api/programs");
