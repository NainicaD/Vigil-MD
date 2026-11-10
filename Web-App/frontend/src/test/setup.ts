// Adds DOM matchers such as toBeInTheDocument() and their TypeScript types to Vitest.
import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(() => cleanup());
