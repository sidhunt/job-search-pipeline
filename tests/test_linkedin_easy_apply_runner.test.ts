import { describe, expect, it } from "bun:test";
import * as path from "node:path";
import * as fs from "node:fs";
import {
  loadJobPayload,
  matchSkillExperience,
  resolveTextValue,
  resolveRadioSelection,
  displayTerminalReviewAlert,
  type JobPayload,
} from "../automation/linkedin_easy_apply_runner";

describe("PRISM KERNEL :: LINKEDIN_EASY_APPLY_RUNNER", () => {
  const mockPayload: JobPayload = {
    jobUrl: "https://www.linkedin.com/jobs/view/9999999999/",
    resumePath: path.resolve("./output/cv_tailored.pdf"),
    profileDir: path.resolve("./.profiles/linkedin_profile"),
    profile: {
      fullName: "Jane Doe",
      email: "jane.doe@example.com",
      phone: "+1-555-555-0123",
      city: "Toronto",
      state: "Ontario",
      country: "Canada",
      postalCode: "M5V 2T6",
      linkedinUrl: "https://www.linkedin.com/in/janedoe",
      githubUrl: "https://github.com/example",
      yearsOfExperience: {
        python: 7,
        typescript: 6,
        react: 6,
        playwright: 4,
        "ci/cd": 5,
      },
      defaultExperienceYears: 5,
      requiresSponsorship: false,
      authorizedInCountry: true,
      willingToRelocate: false,
      comfortableCommuting: true,
    },
  };

  describe("Rule: I:: payload.json (profile+skills) schema & loading", () => {
    it("loads default payload or config fallback", () => {
      const payload = loadJobPayload();
      expect(payload).toBeDefined();
      expect(payload.profile.fullName).toBeDefined();
      expect(payload.profile.email).toBeDefined();
      expect(payload.profile.yearsOfExperience.python).toBeGreaterThanOrEqual(6);
    });

    it("accepts runtime parameter overrides for jobUrl and resumePath", () => {
      const customUrl = "https://www.linkedin.com/jobs/view/88888888/";
      const customResume = "./output/custom_resume.pdf";
      const payload = loadJobPayload(undefined, {
        jobUrl: customUrl,
        resumePath: customResume,
      });

      expect(payload.jobUrl).toBe(customUrl);
      expect(payload.resumePath).toBe(path.resolve(customResume));
    });
  });

  describe("Rule: preserve.prefilled.inputs & simulated.keystroke.delays text resolution", () => {
    it("resolves contact fields correctly", () => {
      expect(resolveTextValue("Phone number", mockPayload)).toBe("+1-555-555-0123");
      expect(resolveTextValue("Mobile Phone", mockPayload)).toBe("+1-555-555-0123");
      expect(resolveTextValue("Email address", mockPayload)).toBe("jane.doe@example.com");
      expect(resolveTextValue("First name", mockPayload)).toBe("Jane");
      expect(resolveTextValue("Last name", mockPayload)).toBe("Doe");
      expect(resolveTextValue("City", mockPayload)).toBe("Toronto");
      expect(resolveTextValue("Postal code", mockPayload)).toBe("M5V 2T6");
    });

    it("resolves portfolio and social URLs", () => {
      expect(resolveTextValue("LinkedIn profile URL", mockPayload)).toBe("https://www.linkedin.com/in/janedoe");
      expect(resolveTextValue("GitHub profile", mockPayload)).toBe("https://github.com/example");
    });


    it("fuzzy matches requested skills to candidate years of experience", () => {
      const skills = mockPayload.profile.yearsOfExperience;
      expect(matchSkillExperience("How many years of Python experience do you have?", skills, 3)).toBe(7);
      expect(matchSkillExperience("TypeScript development (years)", skills, 3)).toBe(6);
      expect(matchSkillExperience("Experience with React.js", skills, 3)).toBe(6);
      expect(matchSkillExperience("Years working with Playwright automation", skills, 3)).toBe(4);
      expect(matchSkillExperience("Unlisted exotic tech stack experience", skills, 5)).toBe(5);
    });

    it("resolves numeric years of experience via resolveTextValue", () => {
      const value = resolveTextValue("How many years of work experience do you have with Python?", mockPayload);
      expect(value).toBe("7");
    });
  });

  describe("Rule: set.radios & legal eligibility resolution", () => {
    it("resolves sponsorship questions to 'No' when requiresSponsorship is false", () => {
      const answer = resolveRadioSelection(
        "Will you now or in the future require visa sponsorship for employment?",
        mockPayload
      );
      expect(answer).toBe("No");
    });

    it("resolves legal work authorization questions to 'Yes' when authorizedInCountry is true", () => {
      const answer = resolveRadioSelection(
        "Are you legally authorized to work in Canada?",
        mockPayload
      );
      expect(answer).toBe("Yes");
    });

    it("resolves commuting preferences accurately", () => {
      expect(resolveRadioSelection("Are you comfortable commuting to this office?", mockPayload)).toBe("Yes");
    });

    it("resolves relocation preferences accurately", () => {
      expect(resolveRadioSelection("Are you willing to relocate for this position?", mockPayload)).toBe("No");
    });
  });

  describe("Rule: human.gate.at.submit (zero.blind.submits)", () => {
    it("formats terminal review alert with mandatory manual submission instructions", () => {
      let output = "";
      const originalLog = console.log;
      console.log = (msg: string) => {
        output += msg + "\n";
      };

      try {
        displayTerminalReviewAlert(mockPayload.jobUrl, mockPayload.resumePath);
        expect(output).toContain("HUMAN REVIEW REQUIRED");
        expect(output).toContain("zero.blind.submits");
        expect(output).toContain(mockPayload.jobUrl);
        expect(output).toContain("MANUALLY click 'Submit application'");
      } finally {
        console.log = originalLog;
      }
    });
  });

  describe("Rule: direct.file.injection & artifact presence", () => {
    it("verifies existence of default cv_tailored.pdf artifact", () => {
      const cvPath = path.resolve("./output/cv_tailored.pdf");
      expect(fs.existsSync(cvPath)).toBe(true);
      const stat = fs.statSync(cvPath);
      expect(stat.size).toBeGreaterThan(1000);
    });
  });
});

  describe("Rule: bridge.ai_job_search.outputs & dashboard integration", () => {
    it("detects portal types accurately from source URLs", () => {
      const { detectPortalType } = require("../automation/linkedin_easy_apply_runner");
      expect(detectPortalType("https://www.linkedin.com/jobs/view/123")).toBe("linkedin");
      expect(detectPortalType("https://boards.greenhouse.io/ada/jobs/456")).toBe("greenhouse");
      expect(detectPortalType("https://jobs.lever.co/cohere/789")).toBe("lever");
      expect(detectPortalType("https://careers.canadatech.example.com/job-01")).toBe("staged_candidate");
      expect(detectPortalType("https://company.com/apply")).toBe("generic");
    });

    it("fetches active applications from localhost dashboard", async () => {
      const { fetchDashboardApplications } = require("../automation/linkedin_easy_apply_runner");
      const { state, draftedRows, allRows } = await fetchDashboardApplications("http://localhost:8765");
      expect(state.ok).toBe(true);
      expect(state.total_rows).toBeGreaterThanOrEqual(100);
      expect(allRows.length).toBeGreaterThanOrEqual(100);
    });

    it("resolves staged CVs for drafted applications", async () => {
      const { fetchDashboardApplications, resolveStagedCv } = require("../automation/linkedin_easy_apply_runner");
      const { state, allRows } = await fetchDashboardApplications("http://localhost:8765");
      const row = allRows.find((r: any) => r.company === "Ada");
      expect(row).toBeDefined();
      const { cvPath, exists } = resolveStagedCv(row!, state.repo_root);
      expect(exists).toBe(true);
      expect(cvPath).toContain("ada-senior-ai-systems-full-stack-engineer");
    });
  });
