// OPTIONAL. client/app.js derives the backend from where the page is served:
// a file:// or localhost page talks to http://localhost:8000, anything else
// talks to the hosted Railway backend. So local development needs no config.js
// at all.
//
// Copy this to config.js only if you need to override that:
//
//   - deploying the client to the gated hosted environment (needs the token)
//   - pointing the local page at some other backend

// window.JUPUS_BACKEND_URL = "http://localhost:8000";
// window.JUPUS_ACCESS_TOKEN = "";   // required by the hosted deployment's gate
