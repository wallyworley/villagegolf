# The Villages Golf Reservation Automation

This project consists of two parts:
1.  **Backend**: A Node.js/Express server (`server.js`) that handles the automation with Puppeteer.
2.  **Frontend**: A React application (`villages-frontend`) that provides a premium UI for the user.

## Prerequisites
-   Node.js installed (v16+ recommended).
-   Google Chrome installed (for Puppeteer).

## Quick Start

You will need **two separate terminal windows** to run the full stack.

### Terminal 1: Backend
The backend listens for requests from the frontend and runs the golf automation.

```bash
# Navigate to the backend directory
cd /Users/walterworley/villages-backend

# Install dependencies (if not already done)
npm install

# Start the server
node server.js
```
*The server will run on port **8080**.*

---

### Terminal 2: Frontend
The frontend is the graphical interface you use in your browser.

```bash
# Navigate to the frontend directory
cd /Users/walterworley/villages-backend/villages-frontend

# Install dependencies (if not already done)
npm install

# Start the development server
npm run dev
```
*The frontend will run on **http://localhost:5173** (or similar).*

## Usage
1.  Open the frontend URL (e.g., `http://localhost:5173`) in your browser.
2.  Select a date from the date picker.
3.  Click **"Find Tee Times"**.
4.  Switch to the **Backend Terminal** to watch the automation logs in real-time.
5.  View the results on the Frontend.
