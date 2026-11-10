import { Route, Routes } from "react-router-dom";
import { ChatProvider } from "./chat/ChatProvider";
import { Layout } from "./components/Layout";
import { DevicePage } from "./pages/DevicePage";
import { SearchPage } from "./pages/SearchPage";

/** Routes only; the router itself is in main.tsx (tests use a MemoryRouter). */
export function App() {
  return (
    <ChatProvider>
      <Layout>
        <Routes>
          <Route path="/" element={<SearchPage />} />
          <Route path="/device/:kind/:entityId" element={<DevicePage />} />
          <Route path="*" element={<SearchPage />} />
        </Routes>
      </Layout>
    </ChatProvider>
  );
}
