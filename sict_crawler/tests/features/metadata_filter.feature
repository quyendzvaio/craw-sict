Feature: Loc va uu tien theo metadata
  Bao criteria AC-R11. Loc cung chi khi caller truyen; suy tu cau hoi chi uu tien mem.

  Scenario: Suy tu cau hoi chi nang diem khong loai
    Given hoi "thong bao tuyen sinh 2025"
    And bai lien quan nhat dang nam 2024
    When retrieval chay khong truyen filters
    Then bai nam 2024 van co trong ket qua

  Scenario: Moi nhat uu tien bai gan day
    Given 2 bai thong bao cung muc do khop dang nam 2022 va 2026
    When hoi "thong bao moi nhat"
    Then bai 2026 dung truoc

  Scenario: Caller truyen filters thi loc cung
    Given filters sections thong-bao va year_from 2025
    When retrieval chay
    Then moi ket qua thuoc muc thong-bao va nam tu 2025

  Scenario: Tai lieu thieu metadata khong bi phat
    Given tai lieu index cu khong co ngay dang
    When ap dung uu tien moi nhat
    Then diem cua tai lieu do giu nguyen
