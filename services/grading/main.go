// Command grading is the Grading service: draft scores computed from submissions, with AI-suggested marks for the
// questions that need the teacher when an assessment asks for them, which a classroom's teacher marks, finalizes
// and releases to students.
package main

import (
	"context"
	"embed"
	"errors"
	"learning/platform"
	"sync/atomic"
)

//go:embed migrations/*.sql
var migrations embed.FS

func main() {
	platform.Serve("grading", migrations, platform.EventMigration, func(a *platform.App) ([]func(context.Context), error) {
		identity := a.Identity()
		server{store: pgStore{a.DB}, access: identity, roster: identity}.routes(a.Mux)
		// Ready only while all nine consumers run: a stalled one would otherwise go unnoticed.
		var v1Running, v2Running, v3Running, v4Running, classroomsRunning, assessmentsRunning, enrollmentsRunning, dueRunning, usersRunning atomic.Bool
		a.Checks = append(a.Checks, func(ctx context.Context) error {
			if !v1Running.Load() || !v2Running.Load() || !v3Running.Load() || !v4Running.Load() || !classroomsRunning.Load() || !assessmentsRunning.Load() || !enrollmentsRunning.Load() || !dueRunning.Load() || !usersRunning.Load() {
				return errors.New("a consumer is stopped")
			}
			return platform.KafkaReady(ctx)
		})
		// Older submission versions keep their consumer groups, so events published before v4 are still graded once.
		submissionsV1 := platform.Consumer{DB: a.DB, Group: "grading-v1"}
		submissionsV2 := platform.Consumer{DB: a.DB, Group: "grading-submitted-v2"}
		submissionsV3 := platform.Consumer{DB: a.DB, Group: "grading-submitted-v3"}
		submissions := platform.Consumer{DB: a.DB, Group: "grading-submitted-v4"}
		classrooms := platform.Consumer{DB: a.DB, Group: "grading-classrooms-v1"}
		assessments := platform.Consumer{DB: a.DB, Group: "grading-assessments-v1"}
		enrollments := platform.Consumer{DB: a.DB, Group: "grading-enrollments-v1"}
		dueDates := platform.Consumer{DB: a.DB, Group: "grading-due-dates-v1"}
		users := platform.Consumer{DB: a.DB, Group: "grading-users-v1"}
		marking := marker{a.DB, aiSuggester{platform.NewAICore()}}
		return []func(context.Context){
			platform.Outbox{DB: a.DB}.Run,
			func(ctx context.Context) { submissionsV1.Consume(ctx, topicSubmittedV1, consume, &v1Running) },
			func(ctx context.Context) { submissionsV2.Consume(ctx, topicSubmittedV2, consume, &v2Running) },
			func(ctx context.Context) { submissionsV3.Consume(ctx, topicSubmittedV3, consume, &v3Running) },
			func(ctx context.Context) { submissions.Consume(ctx, topicSubmittedV4, consume, &v4Running) },
			marking.Run,
			marking.Run,
			func(ctx context.Context) {
				classrooms.Consume(ctx, platform.TopicClassroomDeleted, purge, &classroomsRunning)
			},
			func(ctx context.Context) {
				assessments.Consume(ctx, topicAssessmentDeleted, purgeAssessment, &assessmentsRunning)
			},
			func(ctx context.Context) {
				enrollments.Consume(ctx, platform.TopicEnrollmentRemoved, removeStudent, &enrollmentsRunning)
			},
			func(ctx context.Context) { dueDates.Consume(ctx, topicDueChanged, changeDue, &dueRunning) },
			func(ctx context.Context) { users.Consume(ctx, platform.TopicUserDeleted, purgeUser, &usersRunning) },
		}, nil
	})
}
