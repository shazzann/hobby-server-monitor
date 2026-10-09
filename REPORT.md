# Final Report – Hobby Server Monitor

## 1. Time Spent

I started working on this project on October 6, 2026. I used Claude Code and AI agents to help with development, while I handled the setup, configuration, testing, and reviewing the implementation.

The total active development time was approximately **4.1 hours**, excluding breaks and time spent waiting to resolve environment issues. I recorded the tests and setup checks in [the verification log](docs/verification.md).

Most of the time went into developing the backend, integrating LXD, implementing monitoring, building the dashboard, and testing the application.

I also spent time setting up Google OAuth, installing LXD, fixing issues found during testing, and configuring the services to run using systemd.

## 2. Key Decisions

One of my main priorities was making the application secure and keeping its resource usage low. The main technical decisions are also recorded in [the design decisions](docs/decisions.md).

- **Separating the API from LXD:** I decided not to give the API direct access to the LXD socket because it provides highly privileged access to the host. Instead, I used separate worker and collector services to communicate with LXD.
- **Role-based access control:** I implemented authorization checks so users can only perform actions they are allowed to do. Access is also checked again when background operations are executed.
- **Resource quota management:** Each user has allocated resource limits. Before creating or updating a container, the application checks whether enough quota is available. SQLite transactions help prevent multiple requests from exceeding the limits.
- **Authentication:** I used Google OAuth with server-side sessions. Invitations are also linked to the user's Google account to prevent unauthorized access.
- **Monitoring:** Instead of collecting metrics separately for every dashboard user, a background collector gathers data every 10 seconds. This helps reduce unnecessary resource usage.

## 3. Issues I Faced and How I Solved Them

I faced several issues while implementing and testing the project.

Initially, I had problems setting up LXD because my system was running out of disk space. I had to free up space and complete the environment setup before continuing.

During development, testing and code reviews also revealed several issues related to authentication, request handling, and security. These were fixed and covered with additional tests.

When I tested the application with real LXD containers, I found some problems that were not caught by the initial tests. For example, container uptime was not displayed correctly because of timestamp formatting, and some dashboard operations continued polling even after they had completed.

I also noticed a few problems while testing the user workflows. A new admin with no available quota received a confusing error message, and a container owner could not automatically access their container. I corrected these behaviours and improved the messages shown to users.

There were also some issues while setting up systemd services, mainly related to service permissions and startup ordering.

These issues helped me understand why testing with a real environment is important instead of depending only on automated tests. The relevant checks and fixes are recorded in [the verification log](docs/verification.md).

## 4. What I Learned

This project helped me understand several concepts more clearly.

One important thing I learned was how powerful access to the LXD socket actually is. Even if an application is running as a non-root user, having access to the LXD socket can still give it root-level capabilities. This made me understand the importance of separating privileged operations from the API.

I also learned more about managing concurrent requests. For example, if two users try to allocate resources at the same time, simply checking the available quota is not enough. Using SQLite transactions helps prevent both requests from allocating more resources than allowed.

Another important lesson was handling failed or timed-out operations. If an LXD request times out, it does not necessarily mean the operation failed. The application needs to check the actual container state before deciding what to do next.

I also gained practical experience with LXD resource limits, especially CPU and disk quotas. Some settings behave differently from what I initially expected, so I had to verify them directly.

The monitoring part helped me understand how to handle missing metrics and how to avoid showing incorrect values when data is unavailable.

I also realized that passing unit tests does not guarantee that everything works correctly. Real testing revealed several integration issues that were not identified earlier.

Finally, I learned how to work more effectively with AI coding agents. They helped me develop different parts of the application quickly, but I still needed to provide clear requirements, review the implementation, test the functionality, and fix issues. I found that AI can speed up development significantly, but understanding and verifying the final implementation is still very important.

## 5. Bonus Features Implemented

Apart from the main requirements, I implemented a few additional features:

- Support for adopting existing LXD containers.
- CPU resource limits using hard quotas.
- Process limits and swap restrictions for containers.
- Additional authorization and security checks.
- Background operations with retry-safe request handling and state reconciliation.
- [systemd service configurations](deploy/systemd/) and an [HTTPS deployment configuration](deploy/Caddyfile).
- [GitHub Actions](.github/workflows/ci.yml) for automated checks.
- Additional tests and scripts to verify [service permissions](deploy/check-permissions.sh) and [resource usage](scripts/measure.py).
- Support for re-inviting previously revoked users.

## 6. Resource Measurements

I tested the application on my Windows 11 laptop using WSL2 with Ubuntu 22.04 and LXD.

I measured the CPU and memory usage of the API, background worker, and collector services using [the measurement script](scripts/measure.py).

During a 5-minute test with two running containers and no open dashboard tabs, the systemd services used approximately **0.14% of one CPU core** and **72.2 MiB of memory** ([raw results](measurements/systemd-no-tabs-300s.json)).

With five dashboard tabs open, CPU usage increased to approximately **0.32%**, while memory usage was around **73.9 MiB** ([raw results](measurements/systemd-five-tabs-300s.json)).

The collector's CPU usage remained almost the same because it collected metrics independently of how many users were viewing the dashboard.

These results showed that the application was lightweight in my test environment. However, I only tested it with a small number of containers, so I cannot confirm how it would perform with a much larger workload.

## 7. Known Limitations

There are still some limitations in the current implementation.

- I mainly tested the application with two or three containers on a single host. I have not tested how it performs with a large number of containers.
- HTTPS support is configured, but I only tested the application using localhost.
- The terminal supports individual commands rather than a fully interactive terminal session.
- Reducing memory limits while a container is running can still have certain risks.
- Disk limits can be increased but not safely reduced while using the current storage setup.
- Some external container changes and startup recovery scenarios have not been fully verified.
- Backup and restore procedures are documented but have not been tested.
- Invitations currently use a shareable link instead of automatically sending emails.
- Features such as snapshots, alerts, metric export, a persistent web terminal, and multi-host support are not implemented.

These are areas I would consider improving if I continued developing the project.

## 8. AI Tool Usage

I used **Claude Code (Claude Opus 5.5)** as the main AI development tool for this project.

I started with a development plan and used the AI agent to implement the application in different stages. I also used specialist sub-agents to work on different components in parallel, including the LXD integration, monitoring functionality, and frontend dashboard.

A separate AI reviewer was used to identify possible security problems.

My involvement included planning the project, setting up the development environment, configuring LXD and Google OAuth, testing the application manually, reviewing the results, identifying issues, and guiding the agents through the required fixes.

Using AI agents helped me complete the implementation much faster. However, I also learned that generated code should not be accepted without proper testing and review. The tools used and how I worked with them are documented in [AI usage details](docs/ai-usage.md).
