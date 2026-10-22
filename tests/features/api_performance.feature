Feature: Hieu nang API tren may nho
  Bao criteria AC-P1 den AC-P3. May 2 vCPU 4 GB, 1 tien trinh API.

  Scenario: Cau hoi dang chay khong chan request khac
    Given mot cau hoi /chat dang xu ly
    When goi /media va trang giao dien
    Then ca hai phan hoi ngay khong cho cau hoi xong

  Scenario: Qua tai thi xep hang hoac tu choi tam thoi
    Given so cau hoi dong thoi vuot gioi han
    When them cau hoi moi
    Then cau hoi moi xep hang hoac nhan 503
    And tien trinh API khong het RAM

  Scenario: Chi san sang khi model da nap
    Given API vua khoi dong
    When goi /readyz truoc khi nap xong model
    Then tra chua san sang
    And cau hoi dau tien sau khi san sang khong phai cho nap model
